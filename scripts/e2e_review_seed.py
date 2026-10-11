"""Prépare une base temporaire synthétique pour l'E2E de la page de revue.

Importe une fixture synthétique par l'API réelle (aperçu, association de compte, commit), puis
rattache des contreparties aux paiements par carte : l'import ne crée aucun payee et l'API v1
n'expose pas encore cette action (voir docs/MANUEL_REVUE.md, « Manques d'API »). Ne lit jamais
`csv_references/` ni `data/` ; n'affiche que des noms de marchands synthétiques et des comptages.

Usage : e2e_review_seed.py <fixture.csv> <database.sqlite> <data_dir>
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.db.migrate import upgrade_to_head  # noqa: E402
from app.db.models import Payee, Transaction  # noqa: E402
from app.db.session import write_transaction  # noqa: E402
from app.main import create_app  # noqa: E402

MARKETPLACES = ("AMAZON", "VINTED", "PAYPAL")
HEADERS = {"host": "127.0.0.1", "referer": "http://localhost:5173/transactions"}


def main(fixture: Path, database: Path, data_dir: Path) -> None:
    settings = Settings(database_url=f"sqlite:///{database}", data_dir=data_dir, _env_file=None)
    upgrade_to_head(settings)
    application = create_app(settings)
    with TestClient(application) as client:
        preview = client.post(
            "/api/v1/imports/preview",
            files={"file": ("synthetic.csv", fixture.read_bytes(), "text/csv")},
            headers=HEADERS,
        )
        preview.raise_for_status()
        body = preview.json()
        mapped = client.patch(
            f"/api/v1/imports/{body['preview_id']}/sections/1/account-mapping",
            json={
                "expected_version": body["version"],
                "new_account": {"alias": "Compte revue e2e", "account_role": "current"},
            },
            headers=HEADERS,
        )
        mapped.raise_for_status()
        resolved = mapped.json()
        committed = client.post(
            f"/api/v1/imports/{resolved['preview_id']}/commit",
            json={"expected_version": resolved["version"]},
            headers={"Idempotency-Key": str(uuid4()), **HEADERS},
        )
        committed.raise_for_status()
        if committed.json()["status"] != "committed":
            raise SystemExit("ERREUR : import de la fixture non commité.")

        engine = application.state.db_engine
        attached = 0
        payees: dict[str, Payee] = {}
        with write_transaction(engine) as session:
            for transaction in session.scalars(
                select(Transaction).order_by(Transaction.booked_on, Transaction.transaction_id)
            ):
                lines = [line.strip() for line in transaction.raw_label.splitlines() if line.strip()]
                if len(lines) < 2 or lines[0] not in ("Paiement par carte", "Règlement"):
                    continue
                # La fixture numérote chaque ligne (« X0042 ») : on retire cette référence
                # pour que les achats d'un même marchand partagent une contrepartie.
                merchant = " ".join(lines[1].split()[1:]) if lines[1].startswith("X") else lines[1]
                if not merchant:
                    continue
                payee = payees.get(merchant)
                if payee is None:
                    kind = "marketplace" if any(t in merchant for t in MARKETPLACES) else "merchant"
                    payee = Payee(
                        display_name=merchant,
                        normalized_name=json.dumps(
                            {
                                "entity_kind": kind,
                                "merchant_name": merchant,
                                "operation_type": lines[0],
                                "source_kind": "card",
                                "version": 1,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                    session.add(payee)
                    session.flush()
                    payees[merchant] = payee
                transaction.payee_id = payee.payee_id
                attached += 1
            names = Counter(
                payee.display_name
                for _transaction, payee in session.execute(
                    select(Transaction, Payee).join(Payee, Transaction.payee_id == Payee.payee_id)
                )
            )
    print(json.dumps({"payee_links": attached, "payees": dict(names)}, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
