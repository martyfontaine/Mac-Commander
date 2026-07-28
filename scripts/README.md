# scripts/ — the AppleScript allowlist

Every `.applescript` file here is callable by name:

```
applescript(name="clipboard-read")
applescript(name="clipboard-write", args=["hello"])
```

**You** write these files. The model may only pick one by name and supply
`args`; it cannot author the code that runs. That is the whole point — the
threat model's adversary is a prompt-injected model, not you, so the trust
boundary sits exactly there. It is the same discipline the server already
applies to data (values travel as argv, never interpolated), extended to code.

Adding one is just dropping a file in this directory — no restart is needed,
the catalogue is read per call. Write it with an `on run argv` handler and read
your values from there; never build AppleScript by pasting values into the
text.

Anything in this directory runs unrestricted, so treat adding a file as the
same decision as running it yourself. `applescript()` with no arguments returns
the current catalogue.

## Raw script text

Passing `script=` instead of `name=` is refused unless `config.json` sets
`"allow_raw_applescript": true`. Turning it on restores the pre-audit
behaviour: the model can author arbitrary AppleScript, which reaches the shell
via `do shell script` and can drive any app, including a password manager. See
`AUDIT.md` finding A-001.
