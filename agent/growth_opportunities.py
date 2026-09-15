"""Reuse the existing intent scout's observed signals, without treating sellers as buyers."""
from datetime import datetime,timezone,timedelta
from urllib.request import Request,urlopen
from urllib.parse import urlparse
import json
SCOUT_URL='https://raw.githubusercontent.com/TimonGuldner/locenix-lead-research-agent/main/results/intent_scout_results.json'

def qualify_signal(signal,now):
    try:seen=datetime.fromisoformat(signal['detected_at'].replace('Z','+00:00'))
    except (KeyError,ValueError):return None
    if now-seen>timedelta(days=7):return None
    if signal.get('provider')=='verified_seed':return None
    url=signal.get('source_url','');host=urlparse(url).hostname
    text=(signal.get('title','')+' '+signal.get('snippet','')).lower()
    relevant=any(x in text for x in ('local seo','google maps','google business','unternehmensprofil','review management'))
    buyer=host in ('www.upwork.com','www.freelancer.com','www.reddit.com') and any(x in text for x in ('looking for','need','seeking','suche','gesucht','hiring','ranking boost'))
    directory='verzeichnis' in text or 'directory' in text
    if not relevant or not (buyer or directory):return None
    return {'source':url,'channel':'community' if buyer else 'directory','query_topic':signal.get('query') or signal.get('title'),
        'intent':'buyer_problem' if buyer else 'directory_opportunity','relevance':0.85 if buyer else 0.6,
        'potential_value':'visibility_checks','recommended_action':'Verify current discussion and permitted reply route; offer a check only where requested or permitted' if buyer else 'Check category fit and free listing terms',
        'status':'needs_source_verification','evidence':{'scout_provider':signal.get('provider'),'observed_at':signal['detected_at'],'title':signal.get('title')},
        'audience':'local_business_owners'}

def discover_signals():
    with urlopen(Request(SCOUT_URL,headers={'User-Agent':'LOCENIX-Growth/1.0'}),timeout=30) as r:data=json.load(r)
    now=datetime.now(timezone.utc)
    return [o for s in data.get('signals',[]) if (o:=qualify_signal(s,now))][:8]
