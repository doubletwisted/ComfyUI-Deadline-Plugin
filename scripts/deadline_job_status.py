"""Confirm absent Jobs against fresh repository records, including deleted Jobs."""
import json
from Deadline.Scripting import RepositoryUtils


def __main__(*job_ids):
    records = {}
    for job_id in job_ids:
        job = RepositoryUtils.GetJob(job_id, True)
        if job is not None:
            records[job_id] = {"Status": str(job.JobStatus)}
        else:
            deleted = RepositoryUtils.GetDeletedJob(job_id)
            records[job_id] = {"Status": "Deleted" if deleted is not None else "Missing"}
    print("COMFY_NATIVE_JOBS=" + json.dumps(records))
