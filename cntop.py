#!/usr/bin/env python3
import json
import logging
import os
from collections import namedtuple
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import rados
import rich.box
from ceph_argparse import json_command
from rich.console import Group
from rich.logging import RichHandler
from rich.panel import Panel
from rich.pretty import Pretty
from rich.table import Table
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

CephTarget = Tuple[str, Optional[str]]

LOG = logging.getLogger("cntop")

# TCP INFO fields to show in table (see tcp(7), struct tcp_info in tcp.h)
TCP_INFO_KEYS = [
    "tcpi_total_retrans",
    "tcpi_state",
    "tcpi_rtt_us",
    "tcpi_rttvar_us",
    "tcpi_last_data_recv_ms",
    "tcpi_last_data_sent_ms",
]


def connect() -> rados.Rados:
    conffile = os.getenv("CEPH_CONF") or "/etc/ceph/ceph.conf"
    cluster = rados.Rados(conffile=conffile)
    cluster.connect()
    LOG.info("Connected to cluster %s", cluster.get_fsid())
    return cluster


def get_inventory(cluster: rados.Rados) -> Dict[str, List[CephTarget]]:
    """
    Get Ceph cluster inventory as dict of type -> [target, ...]
    """
    return {
        "osd": [
            ("osd", line.decode("utf-8"))
            for line in json_command(cluster, prefix="osd ls")[1].split(b"\n")
            if line
        ],
        "mon": [
            ("mon", m["name"])
            for m in json.loads(
                json_command(cluster, prefix="mon dump", argdict={"format": "json"})[
                    1
                ].decode("utf-8")
            )["mons"]
        ],
        "mgr": [
            (
                "mgr",
                json.loads(
                    json_command(
                        cluster, prefix="mgr dump", argdict={"format": "json"}
                    )[1].decode("utf-8")
                )["active_name"],
            )
        ],
        # TODO add mds
    }


def ceph_status_kv(cluster: rados.Rados) -> Dict[str, str]:
    """Ceph status as key value pairs. Human readable keys"""
    return {
        "ID": json_command(cluster, prefix="fsid")[1].decode("utf-8").strip(),
        "Health": json_command(cluster, prefix="health")[1].decode("utf-8").strip(),
        "": json_command(cluster, prefix="osd stat")[1].decode("utf-8").strip(),
    }


def format_socket_addr(socket_addr: Dict[str, Any]) -> str:
    if socket_addr["type"] == "none":
        return "∅"
    elif socket_addr["type"] == "any":
        return f"#{str(socket_addr['nonce'])}"
    else:
        return (
            f"{socket_addr['type']}/{socket_addr['addr']}#{str(socket_addr['nonce'])}"
        )


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


def format_connection_type(m: Dict, c: Dict) -> str:
    if c["socket_addr"] in m["my_addrs"]["addrvec"]:
        return "IN"
    else:
        return "OUT"


def format_tcpi_key(raw: str) -> str:
    result = raw.replace("tcpi_", "")
    if result.endswith("_ms") or result.endswith("_us"):
        result = result.replace("_us", "").replace("_ms", "")
    return result.replace("_", " ")


def format_tcpi_unit(k: str) -> str:
    if k.endswith("_ms"):
        return "ms"
    elif k.endswith("_us"):
        return "µs"
    else:
        return ""


def format_tcpi_value(k: str, v: int) -> str:
    if k.endswith("_ms"):
        return format_timedelta_compact(timedelta(milliseconds=v))
    elif k.endswith("_us"):
        return format_timedelta_compact(timedelta(milliseconds=v / 1000))
    else:
        return str(v)


def format_ceph_target(t: Optional[CephTarget]) -> str:
    if t:
        return f"{t[0]}.{t[1]}"
    else:
        return "?.?"


def format_con_crypto(c) -> str:
    if "v2" in c["protocol"]:
        c = c["protocol"]["v2"]["crypto"]
        if c["rx"] == c["tx"]:
            return c["rx"]
        else:
            return "p['rx']/p['tx']"
    else:
        return "-"


def format_con_compression(c) -> str:
    if "v2" in c["protocol"]:
        c = c["protocol"]["v2"]["compression"]
        if c["rx"] == c["tx"]:
            return c["rx"]
        else:
            return "p['rx']/p['tx']"
    else:
        return "-"


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


def discover_messengers(cluster: rados.Rados, target: CephTarget) -> List[str]:
    ret, outbuf, outs = json_command(cluster, target=target, prefix="messenger dump")
    if ret in (0, 1):

        return json.loads(outbuf)["messengers"]
    else:
        LOG.warning(
            "Failed to call messenger dump (list) on %s (%d): %s", target, ret, outs
        )
        return []


def dump_messenger(cluster: rados.Rados, target: CephTarget, msgr: str) -> Any:
    ret, outbuf, outs = json_command(
        cluster,
        target=target,
        prefix="messenger dump",
        argdict={"msgr": msgr, "dumpcontents:all": True, "tcp_info": True},
    )
    if ret in (0, 1):
        return json.loads(outbuf)["messenger"]
    else:
        LOG.warning("Failed to call messenger dump on %s (%d): %s", target, ret, outs)
        return {}


def dump_messengers(
    cluster: rados.Rados, target: CephTarget, msgrs: List[str]
) -> Dict[str, Any]:
    result = {}
    for msgr in msgrs:
        result.update(dump_messenger(cluster, target, msgr))
    return result


def pick_tcp_info(ti: Dict) -> List[str]:
    if ti:
        result = [ti[k] for k in TCP_INFO_KEYS]
        for i, k in enumerate(TCP_INFO_KEYS):
            result[i] = format_tcpi_value(k, result[i])
        return result
    else:
        return [""] * len(TCP_INFO_KEYS)


ConstatRowKey = namedtuple("ConstatRowKey", ["target", "msgr_name", "conn_id"])


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
    ] + [(format_tcpi_key(k), ("tcpi",)) for k in TCP_INFO_KEYS]

    def __init__(self, cluster: rados.Rados, target: Optional[CephTarget], **kwargs):
        super().__init__(**kwargs)
        self.cluster: rados.Rados = cluster
        self._target: Optional[CephTarget] = target
        self.show_tag = "all"
        self.sort_order = "default"
        self.data: Optional[Dict[str, Any]]
        self.messengers = []

    @property
    def target(self) -> Optional[CephTarget]:
        return self._target

    @target.setter
    def target(self, val: CephTarget):
        self._target = val

    def rebuild_columns(self):
        for k in list(self.table.columns.keys()):
            self.table.remove_column(k)
        for col, tags in self.columns:
            if self.show_tag in tags or self.show_tag == "all":
                self.table.add_column(col, key=col)

    def compose(self):
        table = DataTable(cursor_type="row")
        for col, _tags in self.columns:
            table.add_column(col, key=col)
        self.table = table
        with Vertical():
            yield table

    def row_key(self, msgr_name, c) -> str:
        return json.dumps((self._target, msgr_name, c["conn_id"]))

    @classmethod
    def parse_row_key(cls, raw: bytes):
        data = json.loads(raw)
        return ConstatRowKey(target=data[0], msgr_name=data[1], conn_id=data[2])

    def add_con_row(self, msgr_name: str, m: Dict, c: Dict) -> None:
        all_col_row_data = [
            msgr_name,
            str(c["conn_id"]),
            str(c["socket_fd"]) if c["socket_fd"] else "∅",
            str(c["worker_id"]),
            c["state"].replace("STATE_", ""),
            "✔" if c["status"]["connected"] else "𐄂",
            c["peer"]["entity_name"],
            c["peer"]["type"],
            format_con_crypto(c),
            format_con_compression(c),
            next(iter(c["protocol"].values()))["con_mode"],
            (
                f"{c['peer']['global_id']}/{c['peer']['id']}"
                if c["peer"]["id"] != -1
                else "∅"
            ),
            format_connection_type(m, c),
            format_socket_addr(c["socket_addr"]),
            format_socket_addr(c["target_addr"]),
            c["last_active_ago"],
        ] + pick_tcp_info(c["tcp_info"])

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
        LOG.info(self.messengers)

    def refresh_table(self) -> None:
        if not self.table.columns or not self.target:
            return
        self.table.clear()
        self.rebuild_columns()
        self.messengers = discover_messengers(self.cluster, self.target)
        self.data = dump_messengers(self.cluster, self.target, self.messengers)
        for name, m in self.data.items():
            for addr_con in m["connections"]:
                c = addr_con["async_connection"]
                self.add_con_row(name, m, c)
            for c in m["anon_conns"]:
                self.add_con_row(name, m, c)
        self.action_sort(self.sort_order)

    def action_columns(self, tag):
        LOG.info("Showing %s columns", tag)
        self.show_tag = tag
        self.refresh_table()

    def action_sort(self, order):
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

    def __init__(self, cluster: rados.Rados, **kwargs):
        super().__init__(**kwargs)
        self.cluster: rados.Rados = cluster

    async def on_mount(self) -> None:
        self.set_interval(1, self.update_status)

    async def update_status(self) -> None:
        self.data = "\n".join(
            f"{k}\t\t{v}" for k, v in ceph_status_kv(self.cluster).items()
        )

    def render(self) -> str:
        return self.data


class DetailsScreen(ModalScreen):
    """
    Show connection details in a modal overlay
    """

    BINDINGS = [
        Binding("q,esc", "app.pop_screen", "Quit Screen"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(
        self,
        cluster: rados.Rados,
        target: CephTarget,
        msgr_name: str,
        con_id: int,
        get_messenger_data: Callable[[], Optional[Dict[str, Any]]],
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.cluster: rados.Rados = cluster
        self.target: CephTarget = target
        self.msgr_name: str = msgr_name
        self.con_id: int = con_id
        self.get_messenger_data = get_messenger_data
        self.messenger_data = get_messenger_data()

    @classmethod
    def _listens_table(cls, m) -> Group:
        table_addrs = Table(
            show_header=True, box=rich.box.SIMPLE, title="Service Addresses"
        )
        table_addrs.add_column("Proto")
        table_addrs.add_column("Addr")
        table_addrs.add_column("Nonce")
        for addr in m["my_addrs"]["addrvec"]:
            table_addrs.add_row(addr["type"], addr["addr"], str(addr["nonce"]))

        table_listen = Table(
            show_header=True, box=rich.box.SIMPLE, title="Listen Sockets"
        )
        table_listen.add_column("FD")
        table_listen.add_column("Worker")
        for listen in m["listen_sockets"]:
            table_listen.add_row(
                *[
                    str(listen["socket_fd"]),
                    str(listen["worker_id"]),
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
            Panel(Pretty(m["my_name"]), title="my_name"),
            Panel(self._listens_table(m), title="Listen"),
        )

    def get_con_data(self) -> Optional[Dict]:
        if not self.messenger_data:
            return None
        try:
            return next(
                iter(
                    [
                        *[
                            con
                            for con in self.messenger_data[self.msgr_name]["anon_conns"]
                            if con["conn_id"] == self.con_id
                        ],
                        *[
                            con["async_connection"]
                            for con in self.messenger_data[self.msgr_name][
                                "connections"
                            ]
                            if con["async_connection"]["conn_id"] == self.con_id
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
            for k, v in self.messenger_data[self.msgr_name].items()
            if k not in ("connections", "anon_conns")
        }
        return Group(Panel(Pretty(data), title="Messenger"))

    def rich_tcpi(self) -> Group:
        table_tcpi = Table(show_header=True, box=rich.box.SIMPLE, title="TCP Info")
        table_tcpi.add_column("Key")
        table_tcpi.add_column("Value")
        table_tcpi.add_column("Description")

        con = self.get_con_data()
        if con and con.get("tcp_info"):
            for k, v in con.get("tcp_info", {}).items():
                table_tcpi.add_row(
                    format_tcpi_key(k), format_tcpi_value(k, v), get_tcpi_description(k)
                )

        return Group(Panel(table_tcpi, title="TCP Info"))

    def compose(self):
        with ScrollableContainer():
            yield Label(f"Target: {self.target}")
            yield Static(self.rich_messenger_info(), id="msgr_info")
            yield Static(self.rich_tcpi(), id="tcpi")
            yield Static(self.rich_conn_info(), id="conn_info")
            yield Static(self.rich_msgr_info(), id="msgr")
            yield Footer()

    def action_refresh(self):
        LOG.info(
            "Refreshing %s/%s/%s details...",
            format_ceph_target(self.target),
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

    def write(self, message):
        if message.endswith("\n"):
            message = message[:-1]
        self.widget.write(message)

    def flush(self):
        pass


class CephInspectorApp(App):
    BINDINGS = [
        Binding("q,esc", "app.quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
    ]

    CSS_PATH = "ntop.tcss"

    def _inventory_for_select(self):
        return (
            (format_ceph_target(svc), svc)
            for group in get_inventory(self.cluster).values()
            for svc in group
        )

    def compose(self):
        yield Header()
        with Vertical(id="main"):
            yield CephStatus(self.cluster, id="status")
            yield Rule()
            with Horizontal():
                yield Label("Select Ceph Service: ")
                yield Select(
                    self._inventory_for_select(), allow_blank=True, id="service"
                )
            yield Rule()
            yield ConstatTable(self.cluster, None, id="constat")
        yield RichLog(id="log", highlight=True, markup=True)
        yield Footer()

    def __init__(self, cluster, **kwargs):
        super().__init__(**kwargs)
        self.cluster = cluster
        self.dark = False
        self.title = "Ceph Inspector"

    @on(DataTable.RowSelected)
    def show_details(self, event):
        constat = self.query_one(ConstatTable)
        key = constat.parse_row_key(event.row_key.value)

        def update_get_messenger_data():
            constat.refresh_data()
            return constat.data

        LOG.info(
            "Showing details: %s/%s/%s",
            format_ceph_target(key.target),
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
        if (
            event.value is not Select.BLANK
            and isinstance(event.value, tuple)
            and len(event.value) == 2
        ):
            target: CephTarget = event.value
            self.title = f"cntop: {format_ceph_target(event.value)}"
            constat = self.query_one(ConstatTable)
            constat.focus()
            constat.table.focus()
            constat.target = target
            constat.refresh_table()

    def action_refresh(self):
        LOG.info("Refreshing...")
        select = self.query_one(Select)
        select.set_options(self._inventory_for_select())
        constat = self.query_one(ConstatTable)
        constat.refresh_table()

    def on_ready(self):
        log_widget = self.query_one(RichLog)
        log_widget.border_title = "Log"
        self.log_proxy = StreamLogToRichLogProxy(log_widget)
        handler = logging.StreamHandler(self.log_proxy)
        LOG.addHandler(handler)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%Y-%m-%d %H:%M:%S]",
        handlers=[RichHandler(rich_tracebacks=True)],
    )
    cluster = connect()

    def watch_callback(
        arg, line, channel, name, who, stamp_sec, stamp_nsec, seq, level, msg
    ):
        LOG.info(
            "[CEPH] %s | %s: %s",
            channel.decode("utf-8"),
            name.decode("utf-8"),
            msg.decode("utf-8"),
        )

    cluster.monitor_log2("info", watch_callback, 0)

    CephInspectorApp(cluster).run()


if __name__ == "__main__":
    main()
