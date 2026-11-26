"""Kiểm mọi operationId agent dùng (clients/backend.py OPERATIONS) còn tồn tại trong contract backend,
đúng method và path. Chạy lại khi vingo-openapi.yaml đổi.

    uv run python scripts/sync_contract.py [đường dẫn yaml]
"""

import sys
from pathlib import Path

import yaml

from vingo_agent.clients.backend import OPERATIONS

default = Path(__file__).resolve().parents[2] / "vingo-backend/contracts/vingo-openapi.yaml"
source = Path(sys.argv[1]) if len(sys.argv) > 1 else default
spec = yaml.safe_load(source.read_text())

found = {
    op["operationId"]: (method.upper(), path)
    for path, methods in spec["paths"].items()
    for method, op in methods.items()
    if isinstance(op, dict) and "operationId" in op
}
errors = []
for name, operation in OPERATIONS.items():
    if name not in found:
        errors.append(f"thiếu operationId {name}")
    elif found[name] != (operation.method, operation.path):
        errors.append(f"{name}: agent {operation.method} {operation.path} ≠ contract {found[name][0]} {found[name][1]}")
print("\n".join(errors) or f"OK: {len(OPERATIONS)} operation khớp {source.name}")
sys.exit(1 if errors else 0)
