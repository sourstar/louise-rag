"""VeGa, the Vector Gate: receipts in place of a front (docs/notes/vega.md).

Every input Louise sends to a `vega-` model is tagged; VeGa's wrapper, a
LocalAI backend in front of the real one (deploy/vega/), strips the tag,
forwards the input, and writes a RECEIPT binding the tag's id to the run, the
input it received and the vector it returned — before the vector goes back.
Louise accepts the vectors only if she gets both, the embedding and the
receipt, and they agree.

    tag.py       the tag, the input digest, the vector digest — shared, byte
                 for byte, with the wrapper (which carries this package)
    service.py   the receipts service: intake on a unix socket, read-only HTTP
    client.py    Louise's side: tag a batch, verify LocalAI's answer
    __main__.py  `python -m app.vega`: the service on its own

THIS PACKAGE IMPORTS NOTHING HEAVY at its top: the wrapper imports `tag`
inside LocalAI's container, where neither FastAPI nor Louise's platform is.
"""
