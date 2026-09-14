from __future__ import annotations
import json,os

MIN_SAMPLE=int(os.getenv("GROWTH_MIN_SAMPLE","50"))

def recommendation(sample:int, conversions:int, channel:str)->dict:
    rate=(conversions/sample) if sample else 0
    return {"department":"growth","channel":channel,"sample":sample,"conversions":conversions,"conversion_rate":round(rate,4),"eligible_for_reallocation":sample>=MIN_SAMPLE,"rule":f"No automatic channel reallocation below {MIN_SAMPLE} observations."}

def main(): print(json.dumps({"department":"growth","status":"READY","min_sample":MIN_SAMPLE,"responsibilities":["content","channel_traffic","experiments","analytics_learning"],"rule":"recommend from measured data; never fabricate performance"}))
if __name__=="__main__": main()
