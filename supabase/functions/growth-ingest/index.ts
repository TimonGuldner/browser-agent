const url=Deno.env.get("SUPABASE_URL")!;
const key=Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
async function rpc(name:string,data:unknown={}) {
 const r=await fetch(url+"/rest/v1/rpc/"+name,{method:"POST",headers:{"apikey":key,"Authorization":"Bearer "+key,"Content-Type":"application/json"},body:JSON.stringify(data)});
 if(!r.ok)throw new Error("RPC "+name+" HTTP "+r.status);
 return await r.json();
}
let cached:string|undefined;
async function authorized(req:Request){
 const supplied=req.headers.get("x-growth-token");if(!supplied)return false;
 cached??=await rpc("locenix_growth_transport_secret");
 if(!cached)return false;
 const a=new Uint8Array(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(supplied)));
 const b=new Uint8Array(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(cached)));
 let diff=0;for(let i=0;i<a.length;i++)diff|=a[i]^b[i];return diff===0;
}
Deno.serve(async req=>{
 if(req.method!=="POST")return new Response("Method not allowed",{status:405});
 try{
 if(!await authorized(req))return new Response("Unauthorized",{status:401});
 const body=await req.text();if(body.length>2000000)return new Response("Too large",{status:413});
 const {events}=JSON.parse(body);if(!Array.isArray(events)||events.length>5000)return new Response("Invalid batch",{status:400});
 const r=await fetch(url+"/rest/v1/company_runs?status=eq.running&select=id",{headers:{"apikey":key,"Authorization":"Bearer "+key}});
 if(!r.ok)throw new Error("Run lookup failed");
 const runs=await r.json();if(!runs.length)return Response.json({status:"no_active_run"});
 const result=await rpc("company_growth_import",{p_run_id:runs[0].id,p_events:events});
 await rpc("company_record_event",{p_run_id:runs[0].id,p_agent_id:"GROWTH_EXECUTOR",p_department:"ANALYTICS",p_event_type:"analytics.sync_completed",p_status:"completed",p_message:"Product facts synchronized",p_metadata:result});
 return Response.json(result);
 }catch(e){console.error(String(e));return new Response("Growth ingest failed",{status:502});}
});
