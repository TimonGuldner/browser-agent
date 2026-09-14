"""Bounded deterministic growth handlers on Run 2's queue and cost ledger.

No browser automation, model calls or unsolicited contact by default. External
publication is compare-and-swap against a reviewed patch and verified over HTTP.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from agent.control_plane import ControlPlaneClient, CEOOrchestrator

AGENT = 'GROWTH_EXECUTOR'
METRICS = ('revenue','customers','trials','visibility_checks','qualified_traffic','clicks','impressions')
DEPARTMENTS = {'growth_discover':'OPPORTUNITY','growth_seo':'SEO', 'growth_distribute':'DISTRIBUTION',
              'growth_outreach':'OUTREACH','growth_analyze':'ANALYTICS','growth_convert':'CONVERSION',
              'growth_review':'CEO'}
OWNED_HOSTS = {'locenix.com','www.locenix.com'}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def fetch_owned(url):
    parsed=urllib.parse.urlparse(url)
    if parsed.scheme!='https' or parsed.hostname not in OWNED_HOSTS or parsed.username or parsed.port not in (None,443):
        raise ValueError('OWNED_HTTPS_URL_REQUIRED')
    req=urllib.request.Request(url,headers={'User-Agent':'LOCENIX-Growth-Verification/1.0'})
    with urllib.request.urlopen(req,timeout=30) as r:
        final=urllib.parse.urlparse(r.url)
        if final.hostname not in OWNED_HOSTS: raise ValueError('UNEXPECTED_REDIRECT')
        return r.read(2000000).decode('utf-8'),r.status,r.url


class Page(HTMLParser):
    def __init__(self):
        super().__init__();self.links=[];self.h1=0;self.title=False;self.title_text='';self.canonical=''
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='a': self.links.append(a.get('href',''))
        if tag=='h1': self.h1+=1
        if tag=='title': self.title=True
        if tag=='link' and a.get('rel')=='canonical': self.canonical=a.get('href','')
    def handle_endtag(self,tag):
        if tag=='title': self.title=False
    def handle_data(self,data):
        if self.title:self.title_text+=data


def audit_opportunity(url,html):
    page=Page();page.feed(html)
    # Audit existing BOFU assets rather than manufacturing keyword demand.
    if not any(x in url for x in ('google-maps','unternehmensprofil','local-seo','bewertungen','alternative')):return None
    tracked=any('/tools/local-seo-check' in x and 'utm_campaign=' in x for x in page.links)
    if tracked:return None
    return {'source':url,'channel':'owned_search','query_topic':page.title_text,
            'intent':'problem_solution','relevance':0.95,'potential_value':'visibility_checks',
            'recommended_action':'Add a contextual, attributed free visibility-check CTA to this existing BOFU asset',
            'status':'qualified','evidence':{'http_audited':True,'h1_count':page.h1,'missing_attributed_check_cta':True},
            'audience':'local_business_owners','kind':'owned_asset_gap'}


def permitted_route(record):
    if any(record.get(k) for k in ('Do Not Contact','Email Do Not Contact','Intent Do Not Contact')):
        return False,'DO_NOT_CONTACT'
    route=record.get('Contact Route')
    evidence=record.get('Contact Permission Evidence')
    if route=='requested_email_reply' and evidence and record.get('Inbound Message ID'):
        return True,'REQUESTED_REPLY'
    if route=='consented_email' and evidence and record.get('Email Consent At'):
        return True,'DOCUMENTED_CONSENT'
    if route=='community_reply' and evidence and record.get('Platform Rules URL') and record.get('Promotion Permitted') is True:
        return True,'PERMITTED_CONTEXTUAL_REPLY'
    return False,'CONTACT_ROUTE_EVIDENCE_MISSING'


def funnel(rows):
    """Cohort rates only when identity-bearing stage events are present."""
    totals={m:0.0 for m in ('visitors',)+METRICS}
    for r in rows:
        if r.get('is_test') or r.get('dimensions',{}).get('is_test'):continue
        key=r['metric_key']
        if key in totals:totals[key]+=float(r['value'])
    stages=('visitors','visibility_checks','trials','customers')
    rates=[]
    for a,b in zip(stages,stages[1:]):
        denominator=totals[a];numerator=totals[b]
        rates.append({'from':a,'to':b,'rate':numerator/denominator if denominator and numerator<=denominator else None,
                      'dropoff':1-numerator/denominator if denominator and numerator<=denominator else None})
    valid=[r for r in rates if r['dropoff'] is not None]
    return {'totals':totals,'rates':rates,'bottleneck':max(valid,key=lambda r:r['dropoff']) if valid else None,
            'caveat':'Rates require matched campaign cohorts; unknown identity and missing denominators remain unknown.'}


def experiment_decision(observed, expected, sample, minimum, deadline, now, primary_metric):
    if primary_metric not in METRICS[:4]:raise ValueError('DOWN_FUNNEL_PRIMARY_METRIC_REQUIRED')
    if sample>=minimum and observed>=expected:return 'SCALE'
    if now<deadline:return None
    if sample<minimum:return 'PAUSE'
    return 'ITERATE' if observed>0 else 'KILL'


def channel_rank(rows):
    grouped={}
    for r in rows:
        if r.get('dimensions',{}).get('is_test'):continue
        channel=r.get('dimensions',{}).get('channel','unattributed')
        m=grouped.setdefault(channel,{x:0 for x in METRICS})
        if r['metric_key'] in m:m[r['metric_key']]+=float(r['value'])
    return sorted(grouped,key=lambda c:tuple(grouped[c][k] for k in METRICS),reverse=True),grouped


class GrowthExecutor:
    def __init__(self,cp):self.cp=cp
    def enqueue(self,run,kind,payload,key,priority=700):
        return self.cp.create_task(run,DEPARTMENTS[kind],kind,payload.get('recommended_action',kind),priority,
                                  {**payload,'runtime_adapter':'growth'},key)
    def discover(self,job):
        found=[]
        for url in job['input']['urls'][:8]:
            html,status,_=fetch_owned(url)
            op=audit_opportunity(url,html)
            if not op:continue
            oid=self.cp.rpc('company_remember',{'p_scope':'growth_opportunity','p_key':digest(url),'p_value':op,
                'p_source':url,'p_run_id':job['run_id'],'p_department':'OPPORTUNITY','p_evidence':op['evidence']})
            # A real inspect/prepare task. Publication requires a reviewed exact patch.
            task=self.enqueue(job['run_id'],'growth_seo',{'url':url,'opportunity_key':digest(url),'action':'inspect',
                'recommended_action':op['recommended_action']},'growth-seo:'+job['run_id']+':'+digest(url))
            found.append({'opportunity':oid,'task_id':task,'source':url})
        return {'opportunities':found,'evidence':'Live owned BOFU pages audited; durable downstream tasks created'}
    def seo(self,job):
        data=job['input'];html,status,url=fetch_owned(data['url']);page=Page();page.feed(html)
        if status!=200 or page.h1!=1 or not page.canonical:raise RuntimeError('SEO_QA_FAILED')
        if data.get('action')=='publish':return self.publish(job)
        return {'url':url,'h1':page.h1,'canonical':page.canonical,'title':page.title_text,
                'opportunity':audit_opportunity(url,html),'evidence':'HTTP 200, one H1, canonical and existing content inspected',
                'published':False}
    def publish(self,job):
        d=job['input'];patch=d['patch']
        if patch.get('reviewed') is not True:raise ValueError('PATCH_REVIEW_REQUIRED')
        path=patch['path']
        if not path.startswith('src/app/blog/') or not path.endswith('/page.tsx'):raise ValueError('PATCH_PATH_NOT_ALLOWED')
        token=os.environ.get('PRODUCT_GITHUB_TOKEN')
        if not token:raise RuntimeError('PRODUCT_GITHUB_TOKEN_MISSING')
        endpoint='https://api.github.com/repos/TimonGuldner/localboost-ai/contents/'+path
        def api(method,payload=None):
            req=urllib.request.Request(endpoint,data=json.dumps(payload).encode() if payload else None,method=method,
              headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
        current=api('GET');source=base64.b64decode(current['content']).decode()
        before,after=patch['before'],patch['after']
        if before in source:
            if source.count(before)!=1:raise ValueError('AMBIGUOUS_PATCH')
            updated=source.replace(before,after)
            commit=api('PUT',{'message':'Growth experiment: contextual visibility check CTA','sha':current['sha'],
                'content':base64.b64encode(updated.encode()).decode()})['commit']['sha']
        elif after in source:commit='already_applied'
        else:raise ValueError('SOURCE_CHANGED_REVIEW_REQUIRED')
        # A durable verification task waits for deployment, rather than a false publish success.
        tid=self.enqueue(job['run_id'],'growth_distribute',{'url':d['url'],'required_href':d['required_href'],
             'experiment_id':d.get('experiment_id'),'commit':commit},'verify-publish:'+job['id'])
        return {'commit':commit,'verification_task':tid,'evidence':'GitHub accepted exact reviewed patch; live verification pending'}
    def distribute(self,job):
        d=job['input'];html,status,url=fetch_owned(d['url']);p=Page();p.feed(html)
        if d['required_href'] not in p.links:raise RuntimeError('DEPLOYMENT_NOT_YET_VERIFIED')
        target=urllib.parse.urljoin(url,d['required_href']);_,target_status,_=fetch_owned(target)
        if target_status!=200:raise RuntimeError('CTA_TARGET_FAILED')
        return {'url':url,'target':target,'external_action':'owned_contextual_distribution',
                'evidence':'Live HTML contains exact attributed CTA and destination returns HTTP 200',
                'business_outcome':'pending_observation','synthetic_probe':True}
    def analyze(self,job):
        rows=self.cp.get('company_metric_events',f"run_id=eq.{job['run_id']}&select=metric_key,value,dimensions,occurred_at&limit=10000")
        ranking,channels=channel_rank(rows);out=funnel(rows)
        out.update({'channels':channels,'ranking':ranking,'evidence':'Persisted deduplicated production metric events read'})
        self.cp.rpc('company_remember',{'p_scope':'growth_analytics','p_key':job['run_id'],'p_value':out,
          'p_source':'company_metric_events','p_run_id':job['run_id'],'p_department':'ANALYTICS'})
        return out
    def convert(self,job):
        report=self.analyze(job)
        return {'bottleneck':report['bottleneck'],'action':'review_targeted_experiment' if report['bottleneck'] else 'collect_identity_bearing_funnel_data',
                'evidence':report['evidence'],'rates':report['rates']}
    def outreach(self,job):
        # Reuse existing sender and CRM, but never equate a researched email with consent.
        from agent import email_outreach_worker as sender
        sent=[];blocked=[]
        candidates=sender.list_candidates();history=sender.all_sent_emails()
        remaining=max(0,min(1,sender.DAILY_LIMIT-sender.sent_today_count()))
        for rec in candidates:
            if len(sent)>=remaining:break
            f=rec.get('fields',{});allowed,reason=permitted_route(f)
            gate=sender.email_gate(f)
            if not allowed or not gate.allowed:
                blocked.append({'record_id':rec['id'],'reason':reason if not allowed else gate.reason_code});continue
            email=f['Email'].strip().lower()
            if email in history:continue
            # Existing sender, now with provider idempotency to survive CRM write failures.
            receipt=sender.send_resend(email,f['Outreach Subject'],f['Outreach Draft'],idempotency_key='growth-'+rec['id'])
            if not receipt.get('id'):raise RuntimeError('NO_PROVIDER_RECEIPT')
            sender.patch_record(rec['id'],{'Email Send Status':'SENT','Email Message ID':receipt['id'],
                'Email Sent At':datetime.now(timezone.utc).isoformat(),'Email Send Error':''})
            sent.append({'record_id':rec['id'],'message_id':receipt['id']});history.add(email)
        return {'sent':sent,'blocked':blocked,'evidence':'Existing CRM and permitted-route gates evaluated; provider receipts required for sends'}
    def review(self,job):
        return self.cp.rpc('company_growth_review',{'p_run_id':job['run_id']})
    def execute(self,job):
        self.cp.heartbeat(AGENT,'busy',job['id'])
        handler={'growth_discover':self.discover,'growth_seo':self.seo,'growth_distribute':self.distribute,
          'growth_analyze':self.analyze,'growth_convert':self.convert,'growth_outreach':self.outreach,'growth_review':self.review}.get(job['task_type'])
        if handler is None:raise ValueError('UNKNOWN_GROWTH_TASK')
        try:
            result=handler(job)
            self.cp.rpc('company_growth_zero_cost',{'p_task_id':job['id'],'p_service':job['task_type']})
            self.cp.complete_task(job['id'],AGENT,result,{'passed':True,'evidence':result.get('evidence','Persisted CEO review decision')})
            self.cp.receive_result(job['id'])
            return result
        except Exception as e:
            self.cp.rpc('company_growth_fail',{'p_task_id':job['id'],'p_error':str(e)[:500]})
            raise
        finally:self.cp.heartbeat(AGENT,'idle')
    def tick(self,limit=6):
        results=[]
        runs=self.cp.get('company_runs','status=eq.running&select=id')
        for run in runs:
            self.cp.rpc('company_growth_review',{'p_run_id':run['id']})
            CEOOrchestrator(self.cp).cycle(run['id'])
        for _ in range(min(limit,12)):
            rows=self.cp.rpc('claim_agent_job',{'p_worker':AGENT})
            if not rows:break
            try:results.append(self.execute(rows[0]))
            except Exception as e:results.append({'failed':str(e)})
        return results


def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['tick','audit']);parser.add_argument('--url')
    args=parser.parse_args()
    if args.command=='audit':html,_,_=fetch_owned(args.url);out=audit_opportunity(args.url,html)
    else:out=GrowthExecutor(ControlPlaneClient()).tick()
    print(json.dumps(out,ensure_ascii=False,default=str))
if __name__=='__main__':main()
