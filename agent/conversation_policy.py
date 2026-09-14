from __future__ import annotations

BLOCKING_STATES={"NOT_INTERESTED","DO_NOT_CONTACT","INVALID"}

def decision(fields:dict)->dict:
    state=str(fields.get("Lead State") or "").upper()
    dnc=bool(fields.get("Email Do Not Contact") or fields.get("Intent Do Not Contact") or fields.get("Do Not Contact"))
    if dnc or state in BLOCKING_STATES:
        return {"allow_email_followup":False,"allow_linkedin_followup":False,"reason":"cross_channel_stop"}
    replied=state in {"REPLIED","INTERESTED","CHECK_OFFERED","CHECK_REQUESTED","CHECK_COMPLETED","TRIAL","PAID"}
    if replied:
        return {"allow_email_followup":False,"allow_linkedin_followup":False,"reason":"active_conversation_requires_conversation_agent"}
    return {"allow_email_followup":True,"allow_linkedin_followup":True,"reason":"no_cross_channel_block"}
