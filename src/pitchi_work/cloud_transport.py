"""Private one-shot official SDK transport; never prints provider exceptions."""
import sys
from itertools import islice
from pathlib import Path
from .cloud import sdk_client, sdk_generate, error_class, CloudControlStop
from .store import load, dump


def main():
    root = Path(sys.argv[1])
    incoming = load(root/'input.json')
    payload = incoming['payload']
    client = None
    try:
        client = sdk_client(payload['provider'], incoming['timeout'])
        if incoming.get('action') == 'models':
            result = {'ok': True, 'result': {'model_ids': [m.id for m in islice(client.models.list(),1000)]}}
        else:
            result = {'ok': True, 'result': sdk_generate(client, payload)}
    except Exception as exc:
        result = {'ok': False, 'failure_class': exc.failure_class if isinstance(exc, CloudControlStop) else error_class(exc)}
    finally:
        if client is not None: client.close()
    dump(root/'result.json', result)


if __name__ == '__main__': main()
