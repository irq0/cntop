# cntop

A (n)top-like tool to show Ceph "dump_messenger" network information.

Requires patching Ceph

## Usage

```bash
CEPH_CONF=/compile/ceph/build/ceph.conf \
python3 cntop.py
```


Requires rados and ceph_argparse in the python module search path.
If installed locally and ninja installed:

    PYTHONPATH="/usr/lib64/python3.11/site-packages/:/usr/local/lib/python3.11/site-packages/"
