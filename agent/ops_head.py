from __future__ import annotations
import json

ESCALATION={1:"WORKER_RETRY",2:"HEAD_ALTERNATIVE",3:"REPAIR_AGENT",4:"AGENT_0_ESCALATION",5:"HUMAN_REQUIRED"}
HARD={"AUTH_REQUIRED","CAPTCHA","API_LIMIT","PROVIDER_FAILURE","COMPLIANCE_BLOCK","BROWSER_FAILURE"}

def next_level(attempts:int,reason_code:str="")->dict:
    level=min(5,max(1,attempts+1))
    if reason_code in {"AUTH_REQUIRED","CAPTCHA"}: level=max(level,5)
    return {"department":"ops","level":level,"action":ESCALATION[level],"reason_code":reason_code or "WORKER_FAILURE","max_level":5}

def main(): print(json.dumps({"department":"ops","status":"READY","escalation":ESCALATION,"rule":"bounded retries; workers may not fail silently"}))
if __name__=="__main__": main()
