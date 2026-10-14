#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Is every character's workplace intact.

    python3 tools/check.py

Four questions, all of them facts. Nothing here judges whether a character is any good; that is
reading, and reading is a person's job.

    does every character have the files the loop expects
    does every [[link]] point at a note that exists
    is any state file large enough to be a context-window problem
    is every line in an inbox or outbox valid JSON

The third one deserves a word. There is no correct size for a memory file, so this does not
pretend there is: it warns at 16 KB, which is roughly where a state file starts costing more than
it is worth on every single session, and it says the number rather than a verdict. The decision to
compress stays with whoever reads it.
"""
import glob
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS = os.path.join(ROOT, 'agent')

EXPECTED = ('PERSONA.md', 'SESSION_PROMPT.md', 'GOALS.md', 'memory/README.md', 'memory/state.md')
STATE_WARN_BYTES = 16 * 1024
LINK = re.compile(r'\[\[([^\]]+)\]\]')


def characters():
    """A directory under agent/ holding a PERSONA.md is a character. Nothing is hardcoded."""
    out = []
    for d in sorted(glob.glob(os.path.join(AGENTS, '*'))):
        if os.path.isdir(d) and os.path.exists(os.path.join(d, 'PERSONA.md')):
            out.append(os.path.basename(d))
    return out


def main():
    who = characters()
    if not who:
        # Not a pass. No characters found means the tree moved or the shape changed, and calling
        # that "everything is fine" is the failure this file exists to catch.
        print('NOT ESTABLISHED: no character directory under %s holds a PERSONA.md' % AGENTS)
        return 2

    print('CHARACTERS: %s' % ', '.join(who))
    bad, notes = [], []

    for name in who:
        home = os.path.join(AGENTS, name)

        for rel in EXPECTED:
            if not os.path.exists(os.path.join(home, *rel.split('/'))):
                bad.append('%s is missing %s' % (name, rel))

        ents = {os.path.basename(p)[:-3]
                for p in glob.glob(os.path.join(home, 'memory', 'entities', '*.md'))}
        for p in glob.glob(os.path.join(home, 'memory', '**', '*.md'), recursive=True):
            src = os.path.relpath(p, ROOT).replace(os.sep, '/')
            try:
                body = io.open(p, encoding='utf-8').read()
            except OSError:
                continue
            for target in set(LINK.findall(body)):
                # A link to the character itself is how a note says "this involved me", and it
                # needs no file of its own.
                if target == name or target in ents:
                    continue
                bad.append('%s links to [[%s]], which has no note' % (src, target))

        sp = os.path.join(home, 'memory', 'state.md')
        if os.path.exists(sp):
            n = os.path.getsize(sp)
            if n > STATE_WARN_BYTES:
                notes.append('%s state.md is %.1f KB. Nothing is broken; it is the size at which '
                             'compressing pays for itself on every session from now on.'
                             % (name, n / 1024.0))

        for box in ('inbox.jsonl', 'outbox.jsonl'):
            p = os.path.join(home, box)
            if not os.path.exists(p):
                continue
            for i, line in enumerate(io.open(p, encoding='utf-8'), 1):
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except ValueError as e:
                    bad.append('%s/%s line %d is not valid JSON: %s' % (name, box, i, e))

    for n in notes:
        print('   note: %s' % n)
    if bad:
        print()
        for b in bad:
            print('  %s' % b)
        print()
        print('%d problem(s).' % len(bad))
        return 1
    print('Every workplace is intact: files present, links resolve, boxes parse.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
