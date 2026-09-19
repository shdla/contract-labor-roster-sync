"""Send a signed worker.joined test event to the iPaaS webhook.

Builds the event with the real emitter so the body and headers are exactly
what the pipeline produces, then POSTs it.

    export ROSTER_WEBHOOK_URL=https://webhooks.workato.com/webhooks/rest/<id>/worker_joined
    python samples/send_test_event.py --worker 2            # one event
    python samples/send_test_event.py --worker 3 --twice    # dedup demonstration

Use a different --worker (or --period) per test: the event id is
deterministic, so a repeat of the same worker/period is a duplicate by
design and the receiver will stop it.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roster_sync.events import WebhookEventSender, worker_joined_event  # noqa: E402
from roster_sync.models import Worker  # noqa: E402
from roster_sync.normalize import normalize_name  # noqa: E402

WORKERS = {
    1: ("Priya", "Raghunathan", "+18325550227", "buckhoist_operator"),
    2: ("Tomas", "Ruiz", "+18325550214", "material_handler"),
    3: ("Jerome", "Baptiste", "+18325550299", "forklift_operator"),
    4: ("Alicia", "Fontenot", "+18325550288", "pod_production"),
    5: ("Marcus", "Webb", "+18325550142", "material_handler"),
    6: ("Danielle", "Okonkwo", "+18325550178", "pod_production"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("ROSTER_WEBHOOK_URL"), help="webhook URL (or ROSTER_WEBHOOK_URL)")
    parser.add_argument("--secret", default=os.environ.get("ROSTER_SIGNING_SECRET", "dev-secret"))
    parser.add_argument("--worker", type=int, default=1, choices=sorted(WORKERS))
    parser.add_argument("--period", default="2024-07-08", help="roster period date, YYYY-MM-DD")
    parser.add_argument("--twice", action="store_true", help="send the identical event twice")
    parser.add_argument("--dry-run", action="store_true", help="print the payload and headers, do not send")
    args = parser.parse_args()

    first, last, phone, role = WORKERS[args.worker]
    worker = Worker(worker_id=f"{args.worker}" * 8 + "-1111-4111-8111-111111111111",
                    name=normalize_name(first, last), phones={phone}, emails=set(), role=role)
    event = worker_joined_event(worker, date.fromisoformat(args.period))

    sender = WebhookEventSender(args.url or "", args.secret, session=None)
    body = event.body()
    headers = {
        "Content-Type": "application/json",
        sender.dedup_header: event.id,
        sender.signature_header: sender._signature(body),
    }

    print("dedup id  :", event.id)
    print("signature :", headers[sender.signature_header])
    print("payload   :", body.decode())
    if args.dry_run:
        return
    if not args.url:
        sys.exit("no webhook URL: pass --url or set ROSTER_WEBHOOK_URL")

    for attempt in range(2 if args.twice else 1):
        request = urllib.request.Request(args.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                print(f"send #{attempt + 1}: HTTP {response.status} {response.read().decode()}")
        except urllib.error.HTTPError as exc:
            print(f"send #{attempt + 1}: HTTP {exc.code} {exc.read().decode()}")


if __name__ == "__main__":
    main()
