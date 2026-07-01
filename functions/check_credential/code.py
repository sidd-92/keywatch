#input_type_name: CheckCredentialInput
#output_type_name: CheckCredentialResult
#function_name: check_credential

from pydantic import BaseModel
from typing import Optional
from lemma_sdk import FunctionContext, Pod
from datetime import date


class CheckCredentialInput(BaseModel):
    credential_name: str


class CheckCredentialResult(BaseModel):
    found: bool
    credential_name: str
    provider: Optional[str] = None
    expiry_date: Optional[str] = None
    days_left: Optional[int] = None
    status: Optional[str] = None
    owner_slack: Optional[str] = None


async def check_credential(ctx: FunctionContext, data: CheckCredentialInput) -> CheckCredentialResult:
    pod = Pod.from_env()

    response = pod.query(f"SELECT * FROM credentials WHERE name = '{data.credential_name}'")
    items = response.to_dict().get("items", [])

    if not items:
        return CheckCredentialResult(found=False, credential_name=data.credential_name)

    cred = items[0]

    expiry = cred.get("expiry_date")
    days_left = None
    if expiry:
        expiry_date = date.fromisoformat(str(expiry))
        days_left = (expiry_date - date.today()).days

    return CheckCredentialResult(
        found=True,
        credential_name=cred["name"],
        provider=cred.get("provider"),
        expiry_date=str(expiry) if expiry else None,
        days_left=days_left,
        status=cred.get("status"),
        owner_slack=cred.get("owner_slack"),
    )
