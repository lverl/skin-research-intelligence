"""Shared entry point; imports only the selected service."""
import importlib
import os
import sys

if __name__=='__main__':
    block=sys.argv[1] if len(sys.argv)>1 else os.environ.get('BLOCK') or os.environ.get('RAILWAY_SERVICE_NAME','')
    if block not in ('collector','worker','web','bot'):
        sys.exit('Use: python main.py collector|worker|web|bot')
    importlib.import_module(block+'.main').main()
