# cntop

A (n)top-like tool to show Ceph "dump_messenger" network information.

<img width="1736" alt="Screenshot 2024-09-13 at 13 46 28" src="https://github.com/user-attachments/assets/e4903e72-8437-462d-8b26-0f3e6df6cae3">

Requires https://github.com/irq0/ceph/tree/wip/osd-asok-messenger-dump

## Usage

```bash
CEPH_CONF=/compile/ceph/build/ceph.conf \
python3 cntop.py
```


Requires rados and ceph_argparse in the python module search path.
If installed locally and ninja installed:

    PYTHONPATH="/usr/lib64/python3.11/site-packages/:/usr/local/lib/python3.11/site-packages/"
