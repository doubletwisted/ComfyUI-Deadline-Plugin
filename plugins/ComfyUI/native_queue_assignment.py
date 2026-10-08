"""Deadline CLI query with exact .NET task-start ticks for result fencing."""
import json


def __main__(*args):
    from Deadline.Scripting import RepositoryUtils
    job = RepositoryUtils.GetJob(args[0], True)
    task = next(t for t in RepositoryUtils.GetJobTasks(job, True).Tasks if str(t.TaskId) == "0")
    print("COMFY_NATIVE_ASSIGNMENT=" + json.dumps({"worker": str(task.TaskSlaveName),
        "status": str(task.TaskStatus), "start": int(task.TaskStartTime.ToUniversalTime().Ticks)}))
