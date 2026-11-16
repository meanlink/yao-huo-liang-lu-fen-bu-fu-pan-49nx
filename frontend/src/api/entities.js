import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const accounting={
 settlements:()=>api('/accounting/settlements'),
 settlement:id=>api('/accounting/settlements/'+id),
 create:data=>api('/accounting/settlements',{method:'POST',body:JSON.stringify(data)}),
 close:(id,data)=>api('/accounting/settlements/'+id+'/close',{method:'POST',body:JSON.stringify(data||{})}),
 resume:id=>api('/accounting/settlements/'+id+'/resume',{method:'POST'}),
 recompute:id=>api('/accounting/settlements/'+id+'/recompute',{method:'POST'}),
 correct:(id,data)=>api('/accounting/settlements/'+id+'/corrections',{method:'POST',body:JSON.stringify(data)}),
 corrections:()=>api('/accounting/corrections'),
 meters:()=>api('/accounting/meters'),
 reading:(id,data)=>api('/accounting/meters/'+id+'/readings',{method:'POST',body:JSON.stringify(data)}),
 link:data=>api('/accounting/meters/links',{method:'POST',body:JSON.stringify(data)}),
 firings:()=>api('/accounting/firings'),
 saveFiring:data=>api('/accounting/firings',{method:'POST',body:JSON.stringify(data)})};
