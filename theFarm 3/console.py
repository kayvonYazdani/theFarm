import shutil
import textwrap


def _width():
    return max(40, min(88, shutil.get_terminal_size((80, 24)).columns))


def section(title):
    width = _width()
    print("\n" + "─" * width, flush=True)
    print(title, flush=True)
    print("─" * width, flush=True)


def warning(title, *details):
    width = _width()
    label = " Warning · " + title + " "
    print("\n┌" + "─" * (width - 2) + "┐", flush=True)
    for item in (label.strip(), *details):
        for paragraph in str(item).splitlines() or [""]:
            for line in textwrap.wrap(
                paragraph,
                width=width - 4,
                break_long_words=True,
                break_on_hyphens=False,
            ) or [""]:
                print("│ " + line.ljust(width - 4) + " │", flush=True)
    print("└" + "─" * (width - 2) + "┘\n", flush=True)
