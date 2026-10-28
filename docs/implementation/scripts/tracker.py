#!/usr/bin/env python3
"""Simple auditable progress helper for Synq signaling pack. No repo modifications."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
from datetime import datetime, timezone
P=Path(__file__).resolve().parents[1]/'tracking'/'progress.json'
ALLOWED={'NOT_STARTED','IN_PROGRESS','READY_FOR_REVIEW','DONE','BLOCKED'}

def load():return json.loads(P.read_text())
def store(d): P.write_text(json.dumps(d,indent=2)+'\n')
def display(t):return f"{t['id']:<7} {t['status']:<17} {t['title']}"

def main():
    a=argparse.ArgumentParser();sub=a.add_subparsers(dest='action',required=True)
    sub.add_parser('status');sub.add_parser('next');s=sub.add_parser('set');s.add_argument('id');s.add_argument('status',choices=sorted(ALLOWED));s.add_argument('--report');s.add_argument('--commits',help='JSON dictionary {repo:commitSHA}')
    args=a.parse_args();d=load();tasks=d['tasks'];by={t['id']:t for t in tasks}
    if args.action=='status':
        for t in tasks:print(display(t))
    elif args.action=='next':
        block=[x['id'] for x in tasks if x['status']=='BLOCKED']
        if block:print('BLOCKED:', ', '.join(block));sys.exit(1)
        ready=[t for t in tasks if t['status']=='NOT_STARTED' and all(by[x]['status']=='DONE' for x in t['dependencies'])]
        for t in ready:print(display(t))
    elif args.action=='set':
        if args.id not in by:sys.exit(f'Unknown task: {args.id}')
        t=by[args.id]
        if args.status in ('DONE','READY_FOR_REVIEW'):
            missing=[x for x in t['dependencies'] if by[x]['status']!='DONE']
            if missing:sys.exit('Incomplete dependencies: '+', '.join(missing))
            if not args.report or not (P.parents[1]/args.report).is_file():sys.exit('Existing report required for DONE or READY_FOR_REVIEW')
        if args.status=='DONE' and not args.commits:
            sys.exit('DONE requires --commits JSON, use {} only for a genuinely documentation-only task')
        if args.commits:
            try: comm=json.loads(args.commits)
            except ValueError as e:sys.exit(str(e))
            if not isinstance(comm,dict):sys.exit('Commits must be JSON object')
            t['commits']=comm
        if args.report:t['report']=args.report
        t['status']=args.status;t['last_updated']=datetime.now(timezone.utc).isoformat()
        store(d);print(display(t))
if __name__=='__main__':main()
