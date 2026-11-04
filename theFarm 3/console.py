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


class Terminal:
    """Plain terminal dashboard; only measured clip counts are shown as progress."""
    labels = ('Finding Videos', 'VideoHighlighter', 'Gemini Analysis', 'Video Editing', 'Final Output')

    def show(self, *, source='', phase=0, status='RUNNING', counts=None, detail=''):
        import sys
        if not sys.stdout.isatty():
            signature=(source,phase,status,tuple((counts or {}).items()),detail)
            if getattr(self,'last',None)==signature: return
            self.last=signature
            if not getattr(self,'started',False):
                print('THE FARM — AUTOMATED VIDEO EDITOR')
                self.started=True
            label=self.labels[min(phase,4)]
            summary=' | '.join(f'{key}: {value}' for key,value in (counts or {}).items())
            print(f'{status} | {label}' + (f' | {source}' if source else ''))
            if detail: print(detail)
            if summary: print(summary)
            sys.stdout.flush()
            return
        if sys.stdout.isatty():
            print('\033[2J\033[H', end='')
        width = _width()
        print('=' * width)
        print('THE FARM — AUTOMATED VIDEO EDITOR'.center(width))
        print('=' * width)
        print('Status:', status)
        for index, label in enumerate(self.labels):
            state = 'COMPLETE' if index < phase else status if index == phase else 'PENDING'
            print(f'[{index+1}/5] {label:<22} {state}')
        if source: print('Current source:', source)
        if counts:
            print(' | '.join(f'{key}: {value}' for key,value in counts.items()))
            total = counts.get('Selected',0)
            done = counts.get('Completed',0) + counts.get('Rejected',0)
            if total:
                filled = min(24,24*done//total)
                print('['+'#'*filled+'-'*(24-filled)+f'] {done}/{total} clips resolved')
        if detail: print(detail)
        print('=' * width,flush=True)
