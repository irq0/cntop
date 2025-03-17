# cntop

A (n)top-like tool to show Ceph "dump_messenger" network information.

![Screenshot 2024-09-13 at 13 46 28](https://github.com/user-attachments/assets/e4903e72-8437-462d-8b26-0f3e6df6cae3)

## Dependencies

- [Ceph PR #59780](https://github.com/ceph/ceph/pull/59780)
- [Python librados bindings](https://docs.ceph.com/en/latest/rados/api/python/)

## Usage

Point either `--conf` or the environment variable `CEPH_CONF` to your cluster's `ceph.conf`

```bash
./cntop.py --conf ceph.conf
```

To add local admin sockets use `--asok`:

```bash
./cntop.py --asok /ceph/wip/out/radosgw.8000.asok
```

## Keys

Use TAB to switch between service selector, ntop table and log
