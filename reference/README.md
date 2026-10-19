# Reference material (read-only input for the builder)

- `sys-botbase-src/`: main.c, commands.c, util.c from olliz0r/sys-botbase
  (the command parser and the HID/screenshot implementation). The full
  source is at https://github.com/olliz0r/sys-botbase; `make sysmodule`
  clones it at a pinned revision.
- `sys-botbase-2.5/`: the release artifact of sys-botbase v2.5 exactly as
  it is meant to land on the card under
  `/atmosphere/contents/430000000000000B/` (`exefs.nsp` plus
  `flags/boot2.flag`). The install helper may offer it when no local build
  exists. Third-party binary; provenance: the v2.5 release zip.
- `pad.py`: the prototype client written against the real console's
  expected behaviour (never run there yet). Its HOME-menu sequence is the
  starting point for the launch step.
- `RIG-cold-start-excerpt.md`: the section of the rig's manual that
  describes the cold-start problem and the pieces already installed on the
  console.
