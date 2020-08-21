import boto3, time, threading, os, datetime
from app.utils import is_dev, vars
from fastapi_sqlalchemy import db
import socket

# https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2.html
ec2_client = boto3.client('ec2')

def _fetch_status():
    sql = """
    -- Ensure 1 row exists
    insert into jobs_status (id, status, ts_client, ts_svc, svc) 
        values (1, 'off', now(), now(), null) 
        on conflict (id) do nothing;
    update jobs_status set ts_client=now();
    select status, svc,
        extract(epoch FROM (now() - ts_svc)) as elapsed_svc,
        extract(epoch FROM (now() - ts_client)) as elapsed_client
    from jobs_status;
    """
    res = db.session.execute(sql)
    db.session.commit()
    return res.fetchone()


def ec2_up():
    if is_dev(): return
    try:
        ec2_client.start_instances(InstanceIds=[vars.GPU_INSTANCE])
    except: pass


debounce = [datetime.datetime.now(), None]
def jobs_status():
    global debounce

    # debounce client (race-condition, perf)
    if debounce[1] and (datetime.datetime.now() - debounce[0]).total_seconds() < 3:
        return debounce[1]
    res = _fetch_status()
    debounce[0], debounce[1] = datetime.datetime.now(), res

    # job service is fresh (5s)
    if res.elapsed_svc < 5: pass
    # desktop was recently active; very likely  will be back soon
    elif res.elapsed_svc < 300 and res.svc == 'DESKTOP-RD4B4G9': pass
    # jobs svc stale (pending|off), decide if should turn ec2 on
    else:
        # status=on if server not turned off via ec2_down_maybe
        db.session.execute("update jobs_status set status='pending'")
        db.session.commit()
        if res.status in ['off', 'on']:
            threading.Thread(target=ec2_up).start()
    return res.status


# already threaded since in cron job
def ec2_down_maybe():
    with db():
        res = _fetch_status()
        # turn off after 5 minutes of inactivity. Note the client setInterval will keep the activity fresh while
        # using even if idling, so no need to wait long after
        if res.elapsed_client / 60 < 5 or res.status == 'off':
            return
        db.session.execute("update jobs_status set status='off', ts_client=now()")
        db.session.commit()
        if is_dev(): return
        try:
            ec2_client.stop_instances(InstanceIds=[vars.GPU_INSTANCE])
        except: pass
