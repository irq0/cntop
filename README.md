# cntop

A (n)top-like tool to show Ceph "dump_messenger" network information.

![Screenshot 2024-09-13 at 13 46 28](https://github.com/user-attachments/assets/e4903e72-8437-462d-8b26-0f3e6df6cae3)

[Ceph PR #59780](https://github.com/ceph/ceph/pull/59780)

## Usage

```bash
CEPH_CONF=/compile/ceph/build/ceph.conf \
python3 cntop.py
```

Requires rados and ceph_argparse in the python module search path.
If installed locally and ninja installed:

```bash
PYTHONPATH="/usr/lib64/python3.11/site-packages/:/usr/local/lib/python3.11/site-packages/"
```
