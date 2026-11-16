/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean}} ReportSchedule */
/** @typedef {{id:number,reference:string,name:string,status:string,revision:number,delta_kwh:number,allocated_kwh:number,idle_kwh:number,pending_count:number}} Settlement */
/** @typedef {{id:number,code:string,name:string,group_code:string,reading_interval_seconds:number}} Meter */
export {};
