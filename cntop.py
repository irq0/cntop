#!/usr/bin/env python3
import argparse
import json
import logging
import os
import pathlib
import pickle
import socket
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Any, Literal, NamedTuple, Optional, cast, override

import rados
import rich.box
from pydantic import BaseModel, Field, field_validator
from rich.console import Group
from rich.logging import RichHandler
from rich.panel import Panel
from rich.pretty import Pretty
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    RichLog,
    Rule,
    Select,
    Static,
)

LOG = logging.getLogger("cntop")

CEPH_COMMAND_TIMEOUT_SECONDS = 0

# TCP INFO fields to show in table (see tcp(7), struct tcp_info in tcp.h)
# Note: unit prefix is cut off during JSON parsing. See TCPInfo class
TCP_INFO_KEYS = [
    "tcpi_total_retrans",
    "tcpi_state",
    "tcpi_rtt",
    "tcpi_rttvar",
    "tcpi_last_data_recv",
    "tcpi_last_data_sent",
]


class EntityNameT(BaseModel):
    type: str
    num: int


class EntityName(BaseModel):
    type: int
    id: str


class EntityAddr(BaseModel):
    type: str
    addr: str
    nonce: int

    def human(self) -> str:
        if self.type == "none":
            return "∅"
        elif self.type == "any":
            return f"#{str(self.nonce)}"
        else:
            return f"{self.type}/{self.addr}#{str(self.nonce)}"


class AddrVec(BaseModel):
    addrvec: list[EntityAddr]


class Socket(BaseModel):
    socket_fd: int | None
    worker_id: int | None


class DispatchQueue(BaseModel):
    length: int
    max_age_ago: str  # TODO support utimespan_str


class ConnectionStatus(BaseModel):
    connected: bool
    loopback: bool

    def connected_human(self) -> str:
        return "✔" if self.connected else "𐄂"


def format_timedelta_compact(d: timedelta) -> str:
    total_sec = d.total_seconds()
    if total_sec >= 1:
        return f"{total_sec:.3f} s"
    elif total_sec >= 1e-3:
        return f"{total_sec * 1e3:.3f} ms"
    elif total_sec == 0:
        return "0"
    else:
        return f"{total_sec * 1e6:.0f} µs"


class TCPInfo(BaseModel):
    tcpi_state: str
    tcpi_retransmits: int
    tcpi_probes: int
    tcpi_backoff: int
    tcpi_rto: timedelta = Field(alias="tcpi_rto_us")
    tcpi_ato: timedelta = Field(alias="tcpi_ato_us")
    tcpi_snd_mss: int
    tcpi_rcv_mss: int
    tcpi_unacked: int
    tcpi_lost: int
    tcpi_retrans: int
    tcpi_pmtu: int
    tcpi_rtt: timedelta = Field(alias="tcpi_rtt_us")
    tcpi_rttvar: timedelta = Field(alias="tcpi_rttvar_us")
    tcpi_total_retrans: int
    tcpi_last_data_sent: timedelta = Field(alias="tcpi_last_data_sent_ms")
    tcpi_last_ack_sent: timedelta = Field(alias="tcpi_last_ack_sent_ms")
    tcpi_last_data_recv: timedelta = Field(alias="tcpi_last_data_recv_ms")
    tcpi_last_ack_recv: timedelta = Field(alias="tcpi_last_ack_recv_ms")
    tcpi_options: list[str]

    @field_validator("tcpi_rto", "tcpi_ato", "tcpi_rtt", "tcpi_rttvar", mode="before")
    @classmethod
    def us_timedelta(cls, value: int) -> timedelta:
        return timedelta(milliseconds=value / 1000)

    @field_validator(
        "tcpi_last_data_sent",
        "tcpi_last_ack_sent",
        "tcpi_last_data_recv",
        "tcpi_last_ack_recv",
        mode="before",
    )
    @classmethod
    def ms_timedelta(cls, value: int) -> timedelta:
        return timedelta(milliseconds=value)

    def human(self, k: str) -> str:
        v = getattr(self, k)
        if v:
            if isinstance(v, timedelta):
                return format_timedelta_compact(v)
            else:
                return str(v)
        else:
            return ""


class Peer(BaseModel):
    entity_name: EntityName
    type: str
    id: int
    global_id: int
    addr: AddrVec

    def human(self):
        return f"{self.global_id}/{self.id}" if self.id != -1 else "∅"


class ProtocolV2Crypto(BaseModel):
    rx: str
    tx: str


class ProtocolV2Compression(BaseModel):
    rx: str
    tx: str


class ProtocolV1(BaseModel):
    state: str
    connect_seq: int
    peer_global_seq: int
    con_mode: Optional[str]


class ProtocolV2(BaseModel):
    state: str
    connect_seq: int
    peer_global_seq: int
    con_mode: Optional[str]
    rev1: bool
    crypto: ProtocolV2Crypto
    compression: ProtocolV2Compression


class Protocol(BaseModel):
    v1: Optional[ProtocolV1] = None
    v2: Optional[ProtocolV2] = None

    def crypto(self) -> str:
        if self.v2:
            crypto = self.v2.crypto
            if crypto.rx == crypto.tx:
                return crypto.rx
            else:
                return f"{crypto.rx}/{crypto.tx}"
        else:
            return "-"

    def compression(self) -> str:
        if self.v2:
            comp = self.v2.compression
            if comp.rx == comp.tx:
                return comp.rx
            else:
                return f"{comp.rx}/{comp.tx}"
        else:
            return "-"

    def mode(self) -> str:
        if self.v2:
            return self.v2.con_mode or ""
        if self.v1:
            return self.v1.con_mode or ""
        return ""


class AsyncConnection(BaseModel):
    state: str
    messenger_nonce: int
    status: ConnectionStatus
    socket_fd: int | None
    tcp_info: TCPInfo | None
    conn_id: int
    peer: Peer
    last_connect_started_ago: str  # TODO support timepan_str
    last_active_ago: str
    recv_start_time_ago: str
    last_tick_id: int
    socket_addr: EntityAddr
    target_addr: EntityAddr
    port: int
    protocol: Protocol
    worker_id: int


class Connection(BaseModel):
    addrvec: list[EntityAddr]
    async_connection: AsyncConnection


class Messenger(BaseModel):
    nonce: int
    my_name: EntityNameT
    my_addrs: AddrVec
    listen_sockets: list[Socket] = []
    dispatch_queue: DispatchQueue
    connections_count: int
    connections: list[Connection]
    anon_conns: list[AsyncConnection]
    accepting_conns: list[AsyncConnection]
    deleted_conns: list[AsyncConnection]
    local_connection: list[AsyncConnection]

    def direction(self, connection: AsyncConnection):
        if connection.socket_addr in self.my_addrs.addrvec:
            return "IN"
        else:
            return "OUT"


class MessengerDump(BaseModel):
    name: str
    messenger: Messenger


class CephTargetBase(BaseModel):
    class Config:
        frozen = True


class CephOSDTarget(CephTargetBase):
    type: Literal["osd"]
    id: int

    def to_tuple(self):
        return (self.type, self.id)

    @override
    def __str__(self) -> str:
        return f"osd.{self.id}"


class CephMonTarget(CephTargetBase):
    type: Literal["mon"]
    name: str

    def to_tuple(self):
        return (self.type, self.name)

    @override
    def __str__(self) -> str:
        if self.name:
            return f"mon.{self.name}"
        else:
            return "mon"


class CephMgrTarget(CephTargetBase):
    type: Literal["mgr"]
    name: str

    def to_tuple(self):
        return (self.type, self.name)

    @override
    def __str__(self) -> str:
        return f"mgr.{self.name}"


class CephAsokTarget(CephTargetBase):
    type: Literal["asok"]
    path: pathlib.Path

    def to_tuple(self):
        return (self.type, self.path)

    @override
    def __str__(self) -> str:
        return f"ASOK:{self.path}"


CephTarget = Annotated[
    CephOSDTarget | CephMonTarget | CephMgrTarget | CephAsokTarget,
    Field(discriminator="type"),
]


def connect(conffile: pathlib.Path) -> rados.Rados:
    cluster = rados.Rados(conffile=conffile.as_posix())
    cluster.connect()
    LOG.info("Connected to cluster %s", cluster.get_fsid())
    return cluster


class CephCommandError(Exception):
    pass


def asok_command(path: pathlib.Path, cmd: str):
    cmd += "\0"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(path.as_posix())
        LOG.debug("ASOK: %s --> %s", path, cmd)
        sock.sendall(cmd.encode("utf-8"))
        response_bytes = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response_bytes += chunk
        LOG.debug("ASOK: %s <-- %s", path, response_bytes)
    if b"ERROR:" in response_bytes:
        raise CephCommandError(f'Ceph asok command "{cmd}" failed: {response_bytes}')
    return 0, response_bytes[4:], b""


def target_command(
    target: CephTarget, cluster: rados.Rados, cmd: str
) -> tuple[str, str]:
    match target:
        case CephOSDTarget(type="osd", id=osdid):
            ret, outs, outbuf = cluster.osd_command(
                osdid=osdid, cmd=cmd, inbuf=b"", timeout=CEPH_COMMAND_TIMEOUT_SECONDS
            )
        case CephMonTarget(type="mon", name=monid):
            ret, outs, outbuf = cluster.mon_command(
                cmd=cmd, inbuf=b"", timeout=CEPH_COMMAND_TIMEOUT_SECONDS, target=monid
            )
        case CephMgrTarget(type="mgr", name=mgr):
            ret, outs, outbuf = cluster.mgr_command(
                cmd=cmd, inbuf=b"", timeout=CEPH_COMMAND_TIMEOUT_SECONDS, target=mgr
            )
        case CephAsokTarget(type="asok", path=path):
            ret, outs, outbuf = asok_command(path, cmd)

    LOG.debug("cmd %r ret: %r", cmd, ret)

    if ret == 0:
        if isinstance(outs, bytes):
            outs = outs.decode("utf-8")
        if isinstance(outbuf, bytes):
            outbuf = outbuf.decode("utf-8")
        return outs, outbuf
    raise CephCommandError(f'Ceph command "{cmd}" failed with {ret}: {outs}')


def command_outs(
    cluster: rados.Rados,
    target: CephTarget = CephMonTarget(type="mon", name=""),
    **kwargs: Any,
) -> str:
    outs, _ = target_command(target, cluster, json.dumps(kwargs))
    return outs.strip()


def command_json(
    cluster: rados.Rados,
    target: CephTarget = CephMonTarget(type="mon", name=""),
    **kwargs: Any,
) -> Any:
    kwargs["format"] = "json"
    outs, _ = target_command(target, cluster, json.dumps(kwargs))
    try:
        j = json.loads(outs)
    except json.JSONDecodeError as ex:
        LOG.error("JSON parse failed: %s", ex, exc_info=True)
        ex.add_note(outs)
        raise
    return j


def command_lines(
    cluster: rados.Rados,
    target: CephTarget = CephMonTarget(type="mon", name=""),
    **kwargs: Any,
) -> list[str]:
    outs, _ = target_command(target, cluster, json.dumps(kwargs))
    return [line for line in outs.splitlines() if line]


def get_inventory(cluster: rados.Rados) -> dict[str, list[CephTarget]]:
    """
    Get Ceph cluster inventory as dict of type -> [target, ...]
    """
    return {
        "osd": [
            CephOSDTarget(type="osd", id=int(osd))
            for osd in command_lines(cluster, prefix="osd ls")
        ],
        "mon": [
            CephMonTarget(type="mon", name=m["name"])
            for m in command_json(cluster, prefix="mon dump")["mons"]
        ],
        "mgr": [
            CephMgrTarget(
                type="mgr", name=command_json(cluster, prefix="mgr dump")["active_name"]
            )
        ],
        # TODO add mds
    }


def ceph_status_kv(cluster: rados.Rados) -> dict[str, str]:
    """Ceph status as key value pairs. Human readable keys"""
    try:
        return {
            "ID": command_outs(cluster, prefix="fsid"),
            "Health": command_outs(cluster, prefix="health"),
            "": command_outs(cluster, prefix="osd stat"),
        }
    except CephCommandError as ex:
        return {"ID": "", "Health": "", "": f"Error: {ex}"}


def get_tcpi_description(k: str) -> str:
    return {
        "tcpi_retransmits": "current retransmits",
        "tcpi_retrans": "retransmitted segments",
        "tcpi_total_retrans": "total retransmissions over connection lifetime",
        "tcpi_probes": "number of keepalive probes sent",
        "tcpi_backoff": "current backoff values for retransmissions",
        "tcpi_rto_us": "retransmission timeout",
        "tcpi_ato_us": "ack timeout",
        "tcpi_snd_mss": "max segment size for sending",
        "tcpi_rcv_mss": "max segment size for receiving",
        "tcpi_unacked": "number of unack'ed segments",
        "tcpi_lost": "number of segments considered lost",
        "tcpi_pmtu": "path max transmission unit",
        "tcpi_rtt_us": "round trip time",
        "tcpi_rttvar_us": "round trip time variance",
        "tcpi_last_data_sent_ms": "time since the last data was sent",
        "tcpi_last_ack_sent_ms": "time since the last ack was sent",
        "tcpi_last_data_recv_ms": "time since the last data was received",
        "tcpi_last_ack_recv_ms": "time since the last ack was received",
    }.get(k, "")


def discover_messengers(cluster: rados.Rados, target: CephTarget) -> list[str]:
    try:
        return command_json(cluster, target, prefix="messenger dump")["messengers"]
    except CephCommandError:
        LOG.error(
            'Failed to discover messengers on %s. "messenger dump" supported?',
            target,
        )
        return []


def dump_messenger(
    cluster: rados.Rados, target: CephTarget, msgr: str
) -> Messenger | None:
    try:
        return MessengerDump.model_validate_json(
            target_command(
                target,
                cluster,
                json.dumps(
                    {
                        "prefix": "messenger dump",
                        "msgr": msgr,
                        "tcp_info": True,
                        "dumpcontents:all": True,
                    }
                ),
            )[0]
        ).messenger
    except CephCommandError as ex:
        LOG.error('Messenger "%s" dump on %s failed: %s', msgr, target, ex)
        return None


def dump_messengers(
    cluster: rados.Rados, target: CephTarget, msgrs: list[str]
) -> dict[str, Messenger]:
    result: dict[str, Messenger] = {}
    for msgr in msgrs:
        dump = dump_messenger(cluster, target, msgr)
        if dump:
            result[msgr] = dump
    return result


def pick_tcp_info(ti: TCPInfo | None) -> list[str]:
    if ti:
        return [ti.human(k) for k in TCP_INFO_KEYS]
    else:
        return [""] * len(TCP_INFO_KEYS)


class ConstatTable(Widget):
    """Connection status table. One messenger connection per line"""

    BINDINGS = [
        ("a", "columns('all')", "all columns"),
        ("t", "columns('tcpi')", "tcpi columns"),
        ("d", "columns('addr')", "address columns"),
        ("p", "columns('type')", "type columns"),
        ("S", "sort('default')", "Sort by Msgr, Conn#"),
        ("F", "sort('fd')", "Sort by FD"),
        ("W", "sort('worker')", "Sort by Worker"),
    ]

    # ("header", tags for selected column viewing
    # Note: Ensure sort columns are available in every tag view
    columns = [
        ("Messenger", ("tcpi", "addr", "type")),
        ("Conn#", ("tcpi", "addr", "type")),
        (
            "FD",
            (
                "tcpi",
                "addr",
                "type",
            ),
        ),
        ("Worker", ("tcpi", "addr", "type")),
        ("State", ()),
        ("Connected", ()),
        ("Peer: Entity", ()),
        ("Type", ("type")),
        ("Crypto", ("type")),
        ("Compression", ("type")),
        ("Mode", ("type")),
        ("GID", ("type")),
        ("↔", ()),
        ("Local", ("addr",)),
        ("Remote", ("addr",)),
        ("Last Active", ()),
    ] + [(k, ("tcpi",)) for k in TCP_INFO_KEYS]

    RowKey = NamedTuple(
        "ConstatRowKey", [("target", CephTarget), ("msgr_name", str), ("conn_id", int)]
    )

    cluster: rados.Rados
    target: CephTarget | None
    show_tag: str
    sort_order: str
    data: dict[str, Messenger]
    messengers: list[str]
    table: DataTable[str]

    def __init__(self, cluster: rados.Rados, target: CephTarget | None, **kwargs: Any):
        super().__init__(**kwargs)
        self.cluster = cluster
        self.target = target
        self.show_tag = "all"
        self.sort_order = "default"
        self.data = {}
        self.messengers = []

    def rebuild_columns(self) -> None:
        for k in list(self.table.columns.keys()):
            self.table.remove_column(k)
        for col, tags in self.columns:
            if self.show_tag in tags or self.show_tag == "all":
                self.table.add_column(col, key=col)

    def compose(self):
        table: DataTable[str] = DataTable(cursor_type="row")
        for col, _tags in self.columns:
            table.add_column(col, key=col)
        self.table = table
        with Vertical():
            yield table

    def row_key(self, msgr_name: str, c: AsyncConnection) -> str:
        return pickle.dumps((self.target, msgr_name, c.conn_id), 0).decode("ascii")

    @classmethod
    def parse_row_key(cls, raw: str | None) -> RowKey | None:
        if not raw:
            return None
        data = pickle.loads(raw.encode("ascii"))
        return cls.RowKey(target=data[0], msgr_name=data[1], conn_id=data[2])

    def add_con_row(self, msgr_name: str, m: Messenger, c: AsyncConnection) -> None:
        all_col_row_data = [
            msgr_name,
            str(c.conn_id),
            str(c.socket_fd) if c.socket_fd else "∅",
            str(c.worker_id),
            c.state.replace("STATE_", ""),
            c.status.connected_human(),
            c.peer.entity_name.id,
            c.peer.type,
            c.protocol.crypto(),
            c.protocol.compression(),
            c.protocol.mode(),
            c.peer.human(),
            m.direction(c),
            c.socket_addr.human(),
            c.target_addr.human(),
            c.last_active_ago,
        ] + pick_tcp_info(c.tcp_info)

        self.table.add_row(
            *[
                all_col_row_data[i]
                for (i, (_, tags)) in enumerate(self.columns)
                if self.show_tag in tags or self.show_tag == "all"
            ],
            key=self.row_key(msgr_name, c),
        )

    def refresh_data(self):
        if not self.table.columns or not self.target:
            return
        self.messengers = discover_messengers(self.cluster, self.target)
        self.data = dump_messengers(self.cluster, self.target, self.messengers)

    def refresh_table(self) -> None:
        if not self.table.columns or not self.target:
            return
        self.table.clear()
        self.rebuild_columns()
        self.messengers = discover_messengers(self.cluster, self.target)
        self.data = dump_messengers(self.cluster, self.target, self.messengers)
        for name, m in self.data.items():
            for addr_con in m.connections:
                c = addr_con.async_connection
                self.add_con_row(name, m, c)
            for c in m.anon_conns:
                self.add_con_row(name, m, c)
        self.action_sort(self.sort_order)

    def action_columns(self, tag: str):
        LOG.info("Showing %s columns", tag)
        self.show_tag = tag
        self.refresh_table()

    def action_sort(self, order: str):
        self.sort_order = order
        if order == "default":
            self.table.sort("Messenger", "Conn#")
        elif order == "fd":
            self.table.sort("FD")
        elif order == "worker":
            self.table.sort("Worker")


class CephStatus(Static):
    """Ceph cluster status as small textual widget"""

    data = reactive("Fetching ceph status...\n\n")

    def __init__(self, cluster: rados.Rados, **kwargs: Any):
        super().__init__(**kwargs)
        self.cluster: rados.Rados = cluster

    async def on_mount(self) -> None:
        self.set_interval(5, self.update_status)

    async def update_status(self) -> None:
        self.data = "\n".join(
            f"{k}\t\t{v}" for k, v in ceph_status_kv(self.cluster).items()
        )

    def render(self) -> str:
        return self.data


class DetailsScreen(ModalScreen[bool]):
    """
    Show connection details in a modal overlay
    """

    BINDINGS = [
        Binding("q,esc", "app.pop_screen", "Quit Screen"),
        Binding("r", "refresh", "Refresh"),
    ]

    cluster: rados.Rados
    target: CephTarget
    msgr_name: str
    con_id: int
    get_messenger_data: Callable[[], dict[str, Messenger]]
    messenge_data: dict[str, Messenger]

    def __init__(
        self,
        cluster: rados.Rados,
        target: CephTarget,
        msgr_name: str,
        con_id: int,
        get_messenger_data: Callable[[], dict[str, Messenger]],
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.cluster = cluster
        self.target = target
        self.msgr_name = msgr_name
        self.con_id = con_id
        self.get_messenger_data = get_messenger_data
        self.messenger_data = get_messenger_data()

    @classmethod
    def _listens_table(cls, m: Messenger) -> Group:
        table_addrs = Table(
            show_header=True, box=rich.box.SIMPLE, title="Service Addresses"
        )
        table_addrs.add_column("Proto")
        table_addrs.add_column("Addr")
        table_addrs.add_column("Nonce")
        for addr in m.my_addrs.addrvec:
            table_addrs.add_row(addr.type, addr.addr, str(addr.nonce))

        table_listen = Table(
            show_header=True, box=rich.box.SIMPLE, title="Listen Sockets"
        )
        table_listen.add_column("FD")
        table_listen.add_column("Worker")
        for listen in m.listen_sockets:
            table_listen.add_row(
                *[
                    str(listen.socket_fd),
                    str(listen.worker_id),
                ]
            )

        return Group(table_addrs, table_listen)

    def rich_messenger_info(self) -> Group:
        if not self.messenger_data:
            return Group(
                Panel(Pretty("no data"), title="error"),
            )

        m = self.messenger_data[self.msgr_name]
        return Group(
            Panel(
                Text(f"Target: {self.target}, my_name: {m.my_name}"),
                title="Ceph Service",
            ),
            Panel(self._listens_table(m), title="Listen"),
        )

    def get_con_data(self) -> AsyncConnection | None:
        if not self.messenger_data:
            return None
        try:
            return next(
                iter(
                    [
                        *[
                            con
                            for con in self.messenger_data[self.msgr_name].anon_conns
                            if con.conn_id == self.con_id
                        ],
                        *[
                            con.async_connection
                            for con in self.messenger_data[self.msgr_name].connections
                            if con.async_connection.conn_id == self.con_id
                        ],
                    ]
                )
            )
        except StopIteration:
            return None

    def rich_conn_info(self) -> Group:
        return Group(Panel(Pretty(self.get_con_data()), title="Connection"))

    def rich_msgr_info(self) -> Group:
        if not self.messenger_data:
            return Group(Panel(Pretty("no data"), title="Messenger"))

        data = {
            k: v
            for k, v in self.messenger_data.items()
            if k not in ("connections", "anon_conns")
        }
        return Group(Panel(Pretty(data), title="Messenger"))

    def rich_tcpi(self) -> Group:
        table_tcpi = Table(show_header=True, box=rich.box.SIMPLE, title="TCP Info")
        table_tcpi.add_column("Key")
        table_tcpi.add_column("Value")
        table_tcpi.add_column("Description")

        con = self.get_con_data()
        if con and con.tcp_info:
            for k, v in vars(con.tcp_info).items():
                table_tcpi.add_row(k, str(v), get_tcpi_description(k))

        return Group(Panel(table_tcpi, title="TCP Info"))

    def compose(self):
        with ScrollableContainer():
            yield Static(self.rich_messenger_info(), id="msgr_info")
            yield Static(self.rich_tcpi(), id="tcpi")
            yield Static(self.rich_conn_info(), id="conn_info")
            yield Static(self.rich_msgr_info(), id="msgr")
            yield Footer()

    def action_refresh(self):
        LOG.info(
            "Refreshing target=%s msgr=%s conn=%s details...",
            self.target,
            self.msgr_name,
            self.con_id,
        )
        self.messenger_data = self.get_messenger_data()
        self.query_one("#tcpi", expect_type=Static).update(self.rich_tcpi())
        self.query_one("#msgr_info", expect_type=Static).update(
            self.rich_messenger_info()
        )
        self.query_one("#conn_info", expect_type=Static).update(self.rich_conn_info())
        self.query_one("#msgr", expect_type=Static).update(self.rich_msgr_info())


class StreamLogToRichLogProxy:
    def __init__(self, log_widget: RichLog):
        super().__init__()
        self.widget = log_widget

    def write(self, message: str) -> None:
        if message.endswith("\n"):
            message = message[:-1]
        self.widget.write(message)

    def flush(self) -> None:
        pass


class CephInspectorApp(App[bool]):
    BINDINGS = [
        Binding("q,esc", "app.quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
    ]

    CSS_PATH = "ntop.tcss"

    cluster: rados.Rados
    extra_asok: list[pathlib.Path]

    def __init__(
        self, cluster: rados.Rados, extra_asok: list[pathlib.Path], **kwargs: Any
    ):
        super().__init__(**kwargs)
        self.cluster = cluster
        self.dark = False
        self.title = "Ceph Inspector"
        self.extra_asok = extra_asok

    def _inventory_for_select(self) -> list[tuple[str, CephTarget]]:
        inventory = get_inventory(self.cluster)
        inventory["asok"] = [
            CephAsokTarget(type="asok", path=asok) for asok in self.extra_asok
        ]
        return [(str(svc), svc) for group in inventory.values() for svc in group]

    def compose(self):
        select: Select[CephTarget] = Select(
            self._inventory_for_select(), allow_blank=True, id="service"
        )
        yield Header()
        with Vertical(id="main"):
            yield CephStatus(self.cluster, id="status")
            yield Rule()
            with Horizontal():
                yield Label("Select Ceph Service: ")
                yield select
            yield Rule()
            yield ConstatTable(self.cluster, None, id="constat")
        yield RichLog(id="log", highlight=True, markup=True, wrap=True)
        yield Footer()

    @on(DataTable.RowSelected)
    def show_details(self, event: DataTable.RowSelected):
        constat = self.query_one(ConstatTable)
        key = constat.parse_row_key(event.row_key.value)
        if not key:
            return

        def update_get_messenger_data() -> dict[str, Messenger]:
            constat.refresh_data()
            return constat.data

        LOG.info(
            "Showing details: target=%s msgr=%s conn=%s",
            key.target,
            key.msgr_name,
            key.conn_id,
        )
        self.push_screen(
            DetailsScreen(
                self.cluster,
                key.target,
                key.msgr_name,
                key.conn_id,
                update_get_messenger_data,
            )
        )

    @on(Select.Changed)
    def select_changed(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK:
            return
        target = cast(CephTarget, event.value)
        self.title = f"cntop: {target}"
        constat = self.query_one(ConstatTable)
        constat.focus()
        constat.table.focus()
        constat.target = target
        constat.refresh_table()

    def action_refresh(self):
        LOG.info("Refreshing...")
        select = self.query_one(Select[CephTarget])
        select.set_options(self._inventory_for_select())
        constat = self.query_one(ConstatTable)
        constat.refresh_table()

    def on_ready(self):
        log_widget = self.query_one(RichLog)
        log_widget.border_title = "Log"
        self.log_proxy = StreamLogToRichLogProxy(log_widget)
        handler = logging.StreamHandler(self.log_proxy)
        LOG.addHandler(handler)


def path_exists(s: str) -> pathlib.Path:
    p = pathlib.Path(s)
    if not p.exists():
        raise argparse.ArgumentTypeError(f"Path {s} does not exists")
    return p


def parse_args():
    parser = argparse.ArgumentParser("cntop")
    conf_env = os.environ.get("CEPH_CONF")
    if conf_env:
        conf_env = path_exists(conf_env)

    parser.add_argument(
        "--conf",
        type=path_exists,
        help="Ceph configuration. Defaults to CEPH_CONF environment variable",
        default=conf_env,
    )
    parser.add_argument(
        "--asok",
        type=path_exists,
        action="append",
        default=[],
        help="add ceph daemon admin socket",
    )
    parser.add_argument("--debug", action="store_true", help="enable debug logging")

    args = parser.parse_args()
    if not args.conf:
        raise argparse.ArgumentTypeError("No config file specified")
    return args


def main():
    args = parse_args()
    logging.basicConfig(
        level=[logging.INFO, logging.DEBUG][int(args.debug)],
        format="%(message)s",
        datefmt="[%Y-%m-%d %H:%M:%S]",
        handlers=[RichHandler(rich_tracebacks=True)],
    )
    cluster = connect(args.conf)

    def watch_callback(
        _arg: object,
        _line: str,
        channel: bytes,
        name: bytes,
        _who: str,
        _stamp_sec: int,
        _stamp_nsec: int,
        _seq: int,
        _level: str,
        msg: bytes,
    ):
        LOG.info(
            "[CEPH] %s | %s: %s",
            channel.decode("utf-8"),
            name.decode("utf-8"),
            msg.decode("utf-8"),
        )

    cluster.monitor_log2("info", watch_callback, 0)

    CephInspectorApp(cluster, args.asok).run()


if __name__ == "__main__":
    main()
