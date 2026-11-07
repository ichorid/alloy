# sheet — a spreadsheet you drive from the command line

We want a small spreadsheet engine. People will script it from the shell, so
it has no screen: it reads commands, one per line, on standard input and
answers on standard output. It should behave the way people expect a normal
spreadsheet (Excel, LibreOffice, Google Sheets) to behave. Where this page is
silent, do what a spreadsheet user would expect.

## Running it

    python3 -m sheet

reads commands until end of input. Each command is one line. Blank lines are
ignored. Commands are case-insensitive; cell references are too (`a1` is
`A1`).

## Commands

The command words below are fixed: scripts written against them must keep
working.

| Command | Meaning | Output |
|---|---|---|
| `SET <cell> <input>` | put a value or a formula in a cell | nothing |
| `GET <cell>` | show what the cell displays | one line |
| `COPY <from> <to>` | copy a cell (with its formula) to another cell | nothing |
| `INSERTROW <n>` / `DELETEROW <n>` | insert a blank row before row n / delete row n | nothing |
| `UNDO` | undo the last change | nothing |
| `SAVE <path>` / `LOAD <path>` | keep a workbook in a file and get it back | nothing |
| `EXPORT <path>` / `IMPORT <path>` | values as CSV, out and in | nothing |
| `SHEET <name>` | switch to a sheet, creating it if needed | nothing |
| `NAME <name> <range>` | give a range a name usable in formulas | nothing |

A command it does not understand, or cannot carry out, prints one line that
starts with `ERROR` and the session goes on. It must never crash.

## Values and formulas

- An input that starts with `=` is a formula; anything else is a value
  (a number if it looks like one, otherwise text).
- `GET` prints numbers the way a person would write them (`3`, not `3.0`;
  `0.5`), text as is, an empty cell as an empty line.
- Formulas: the usual arithmetic, references to other cells, ranges like
  `A1:B3`, and the common functions people use every day (sums, averages,
  minimum and maximum, counting, conditions, a few text functions). Values
  update when the cells they depend on change.
- Problems show up the way spreadsheets show them, with the usual error codes
  (`#DIV/0!` and friends). A formula that depends on itself must not hang.
- Copying a formula shifts its references like a spreadsheet does, unless they
  are anchored with `$`.

## Later

Workbooks can have several sheets, and formulas can refer to other sheets
(`Sheet2!A1`). Named ranges work anywhere a range does. Big sheets (tens of
thousands of cells) should still feel instant.
