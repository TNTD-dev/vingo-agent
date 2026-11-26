"""G1 Auth (V2.2 mục 5.1): chỉ tin backend `chat`.

Backend gửi X-Agent-Service-Key + danh tính dựng từ phiên (X-Vingo-User-Id, X-Vingo-Role)
+ Authorization: Bearer <session token của user>. Token chỉ dùng trong request, không lưu.
"""

import hmac
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from vingo_agent.config import Settings, get_settings
from vingo_agent.schemas.chat import Role


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    role: Role
    session_token: str  # không log, không lưu

    def __repr__(self) -> str:  # tránh lộ token khi bị in ra
        return f"Principal(user_id={self.user_id}, role={self.role})"


def get_principal(
    settings: Annotated[Settings, Depends(get_settings)],
    service_key: Annotated[str | None, Header(alias="X-Agent-Service-Key")] = None,
    user_id: Annotated[str | None, Header(alias="X-Vingo-User-Id")] = None,
    role: Annotated[str | None, Header(alias="X-Vingo-Role")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if (
        not settings.agent_service_key
        or not service_key
        or not hmac.compare_digest(service_key.encode(), settings.agent_service_key.encode())
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "UNAUTHORIZED")
    if not authorization or not authorization.startswith("Bearer ") or not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "UNAUTHORIZED")
    try:
        parsed_user = UUID(user_id)
        parsed_role = Role(role or "")
    except ValueError as exc:  # role lạ → từ chối
        raise HTTPException(status.HTTP_403_FORBIDDEN, "FORBIDDEN") from exc
    return Principal(parsed_user, parsed_role, authorization.removeprefix("Bearer "))


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
