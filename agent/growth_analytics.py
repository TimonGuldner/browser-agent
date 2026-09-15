"""Import consent-aware product facts; never upgrade clicks/signups into sales."""
from __future__ import annotations
import os
from agent.control_plane import ControlPlaneClient


def import_product(cp,product,run_id):
    rows=product.rpc('locenix_growth_export',{})
    return cp.rpc('company_growth_import',{'p_run_id':run_id,'p_events':rows})



def product_client():
    key=os.environ.get('LOCENIX_PRODUCT_SERVICE_ROLE_KEY')
    return ControlPlaneClient('https://tpnjaoqocbshqykcliuv.supabase.co',key) if key else None
