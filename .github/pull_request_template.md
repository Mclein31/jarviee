## Summary
Briefly describe what changed and why.

## Related Issue
Closes #

## Verification
- Ran `python3 -m unittest discover -s tests -q`: (paste result summary)

## Checklist
- [ ] Core code uses Python 3.9+ standard library only (no third-party pip dependencies)
- [ ] Hook scripts (`hooks/`) remain non-blocking (short timeout, errors swallowed, always exit `0`)
- [ ] Server remains `127.0.0.1`-only with `X-Jarviee-Token` and `Host`/`Origin` checks intact
- [ ] Added or updated unit tests in `tests/`
- [ ] No private paths, emails, tokens, or personal project names included
