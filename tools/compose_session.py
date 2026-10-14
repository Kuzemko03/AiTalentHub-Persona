#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Assemble one session prompt for a character.

    python3 tools/compose_session.py vesper
    python3 tools/compose_session.py vesper --from-operator "stop introducing yourself, it is done"

It reads the character's files and whatever arrived since last time, and prints one prompt to
stdout. Printing to stdout is deliberate: it works with any runner, it is readable by a human, and
it can be diffed between two runs to see exactly what changed.

This file decides nothing. It has no rule about any character and no special case for any
situation, and it must stay that way. The first `if name == "vesper"` added here turns the design
back into a prompt with extra steps, and the copying starts again.

The order matters. What arrived once goes at the very top, because a session that reads only the
first screen still sees it. Then who you are, then what you are doing, then the standing
instruction, then the rules.
"""
import argparse
import datetime
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS = os.path.join(ROOT, 'agent')


def read(*parts):
    try:
        return io.open(os.path.join(*parts), encoding='utf-8').read().strip()
    except OSError:
        return ''


def unanswered(home):
    """Inbox lines with no reply to that person after them.

    Deliberately simple, and simple in a way that fails toward MORE work rather than less: if it
    is unsure, the line is shown. A missed message is a person ignored; a duplicate is a character
    saying one thing twice, which is embarrassing and recoverable.
    """
    import json
    inbox, outbox = [], []
    for name, into in (('inbox.jsonl', inbox), ('outbox.jsonl', outbox)):
        p = os.path.join(home, name)
        if not os.path.exists(p):
            continue
        for line in io.open(p, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            try:
                into.append(json.loads(line))
            except ValueError:
                # A malformed line is said out loud rather than skipped. A silently dropped
                # message is the one failure this whole file exists to avoid.
                into.append({'_broken': line[:120]})
    answered_after = {}
    for o in outbox:
        if o.get('to') and o.get('at'):
            answered_after[o['to']] = max(answered_after.get(o['to'], ''), o['at'])
    out = []
    for m in inbox:
        if m.get('_broken'):
            out.append(m)
            continue
        last = answered_after.get(m.get('from'), '')
        if not last or (m.get('at') or '') > last:
            out.append(m)
    return out


def compose(name, from_operator=None):
    home = os.path.join(AGENTS, name)
    if not os.path.isdir(home):
        print('no such character: %s' % name, file=sys.stderr)
        return None

    L = []
    add = L.append

    if from_operator:
        add('=== NEW FROM THE OPERATOR ===')
        add('')
        add(from_operator)
        add('')
        add('This is delivered exactly once and will not come again. Before anything else, write')
        add('it into GOALS.md as item 0, marked OPEN, and keep it open until it is genuinely')
        add('finished, however many sessions that takes.')
        add('')
        add('=' * 30)
        add('')

    now = datetime.datetime.now()
    add('NOW: %s (%s)' % (now.strftime('%Y-%m-%d %H:%M'), now.strftime('%A')))
    add('')

    pending = unanswered(home)
    if pending:
        add('UNANSWERED (%d). Read the entity note for each person before replying, so you are'
            % len(pending))
        add('continuing a conversation rather than starting one over.')
        add('')
        for m in pending:
            if m.get('_broken'):
                add('- a line in the inbox could not be parsed: %s' % m['_broken'])
            else:
                add('- %s at %s: %s' % (m.get('from'), m.get('at'), m.get('text')))
        add('')

    add('--- WHO YOU ARE ---')
    add('')
    add(read(home, 'PERSONA.md') or '(no PERSONA.md)')
    add('')

    goals = read(home, 'GOALS.md')
    if goals:
        add('--- WHAT YOU ARE DOING ---')
        add('')
        add(goals)
        add('')

    state = read(home, 'memory', 'state.md')
    if state:
        add('--- WHERE YOU LEFT OFF ---')
        add('')
        add(state)
        add('')

    add('--- THIS SESSION ---')
    add('')
    add(read(home, 'SESSION_PROMPT.md') or '(no SESSION_PROMPT.md)')
    add('')

    add('--- THE RULES ---')
    add('')
    add(read(AGENTS, 'AGENTS.md'))
    add('')
    add(read(AGENTS, 'shared', 'rules.md'))
    return '\n'.join(L) + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('character')
    ap.add_argument('--from-operator', default=None,
                    help='a message delivered exactly once, placed at the very top')
    a = ap.parse_args()
    out = compose(a.character, a.from_operator)
    if out is None:
        return 2
    sys.stdout.write(out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
