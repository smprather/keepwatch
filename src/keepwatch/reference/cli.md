# Command-line reference

Global options come before the command: `keepwatch [--config PATH] COMMAND ...`. Every command accepts `-h`/`--help`. Exit status: 0 for success, 1 for problems found or a failed poll, 2 for wrong usage. When stdout is not a terminal, output is plain text with no colour or box drawing; `validate`, `poll`, `status` and `logs` also accept `--json`. Output is written as UTF-8 even when the stream's encoding is not (a Windows pipe defaults to cp1252); the stream is left alone on a terminal, where it is already UTF-8.
