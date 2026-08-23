# UPL upstream

- Repository: https://github.com/mengyp2022-droid/upl
- Commit: `6667e4746e7597e9b7fd77dbc29c717771d9f3f5`
- License: MIT (`LICENSE` in this directory)
- Imported files: payment extractor scripts and their shared environment builder only.

The local application invokes these scripts through
`platforms/chatgpt/upl_adapter.py` in isolated subprocesses. Account tokens are
provided through process environment variables and are not written to UPL's
`token.txt` files.
