import json
import boto3
import os
import re
import time
import urllib3
from botocore.exceptions import ClientError

# === AWS & HTTP Clients ===
sns_client     = boto3.client('sns')
ssm_client     = boto3.client('ssm')
bedrock_client = boto3.client('bedrock-runtime', region_name="us-east-1")
ec2_client     = boto3.client('ec2', region_name="us-east-1")
http           = urllib3.PoolManager()

# === Configuration ===
MODEL_ID          = "anthropic.claude-v2"
SNS_TOPIC_ARN     = "arn:aws:sns:us-east-1:657506130129:SmartOpsAlertTopic"
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
AWS_REGION        = ec2_client.meta.region_name

def lambda_handler(event, context):
    # 0) Prevent recursion if SNS message is our own "remediation" publish
    record = event['Records'][0]['Sns']
    if record.get('MessageAttributes', {}).get('source', {}).get('StringValue') == 'remediation':
        return {'statusCode': 200, 'body': 'skipped'}

    # 1) Parse CloudWatch alarm payload
    sns_msg    = json.loads(record['Message'])
    alarm_name = sns_msg.get('AlarmName', 'UnknownAlarm')
    timestamp  = sns_msg.get('StateChangeTime', 'UnknownTime')

    # 2) Determine if CPU or Memory alarm
    alarm_type = 'cpu'
    if 'memory' in alarm_name.lower():
        alarm_type = 'memory'

    # 3) Extract the EC2 InstanceId
    instance_id = None
    trigger = sns_msg.get('Trigger', {}) or sns_msg.get('trigger', {})
    for d in trigger.get('Dimensions', trigger.get('dimensions', [])):
        dim_name = d.get('Name') or d.get('name')
        if dim_name == 'InstanceId':
            instance_id = d.get('Value') or d.get('value')
            break

    # ==== 1) Fetch EC2 metadata ====
    if instance_id:
        resp = ec2_client.describe_instances(InstanceIds=[instance_id])
        inst = resp['Reservations'][0]['Instances'][0]

        name_tag = next((t['Value'] for t in inst.get('Tags', []) if t['Key']=='Name'), 'N/A')
        inst_type  = inst.get('InstanceType', 'N/A')
        state      = inst.get('State', {}).get('Name', 'N/A')
        az         = inst.get('Placement', {}).get('AvailabilityZone', 'N/A')
        launch_iso = inst.get('LaunchTime').isoformat()
        priv_ip    = inst.get('PrivateIpAddress', 'N/A')
        pub_ip     = inst.get('PublicIpAddress', 'N/A')

        resource_details = (
            f"Name: {name_tag}\n"
            f"InstanceId: {instance_id}\n"
            f"Type: {inst_type}\n"
            f"State: {state}\n"
            f"AZ: {az}\n"
            f"Launched: {launch_iso}\n"
            f"Private IP: {priv_ip}\n"
            f"Public IP: {pub_ip}"
        )

        metrics_url = (
            f"https://{AWS_REGION}.console.aws.amazon.com/cloudwatch/home"
            f"?region={AWS_REGION}#resource-health:dashboards/ec2/{instance_id}?"
            f"~(leadingMetric~'*22{alarm_type}-utilization*22)"
        )
    else:
        resource_details = "No EC2 instance ID found in alarm."
        metrics_url = "N/A"

    # ==== 2) Fetch top 5 processes via SSM ====
    top_processes = "Not available"
    top_process_name = "Unknown"
    if instance_id:
        try:
            if alarm_type == "cpu":
                shell_cmd = "ps -eo pid,comm,%cpu --sort=-%cpu | head -n6"
            else:
                shell_cmd = "ps -eo pid,comm,%mem --sort=-%mem | head -n6"

            cmd = ssm_client.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [shell_cmd]},
                TimeoutSeconds=30
            )
            cmd_id = cmd["Command"]["CommandId"]

            # Poll until the command finishes (up to 8 retries)
            max_retries = 8
            retry_delay = 2
            inv = None
            for attempt in range(max_retries):
                try:
                    inv = ssm_client.get_command_invocation(
                        CommandId=cmd_id,
                        InstanceId=instance_id
                    )
                    if inv["Status"] in ("Success", "Failed", "TimedOut", "Cancelled"):
                        break
                except ClientError as error:
                    if error.response['Error']['Code'] == 'InvocationDoesNotExist':
                        time.sleep(retry_delay)
                        continue
                    else:
                        raise
                time.sleep(retry_delay)

            if inv and inv["Status"] == "Success":
                top_processes = inv["StandardOutputContent"].strip()
                lines = top_processes.splitlines()
                if len(lines) >= 2:
                    first_proc_line = lines[1].strip()
                    parts = first_proc_line.split()
                    if len(parts) >= 2:
                        top_process_name = parts[1]
            elif inv:
                top_processes = f"SSM command status: {inv['Status']}"
            else:
                top_processes = "SSM command did not complete"
        except Exception as e:
            top_processes = f"Error fetching processes via SSM: {e}"

    # ==== 3) Ask Bedrock with explicit markers ====
    metric_unit = "%CPU" if alarm_type == "cpu" else "%MEM"
    prompt = (
        f"Human: A CloudWatch alarm '{alarm_name}' for EC2 instance {instance_id or 'Unknown'} "
        f"fired at {timestamp} due to high {alarm_type.upper()} usage.\n\n"
        f"The top process consuming {metric_unit} is: '{top_process_name}'.\n"
        f"Here are the top 5 processes by {metric_unit}:\n{top_processes}\n\n"
        
        "###ADVICE###\n"
        "Please provide a clear, concise paragraph of human-friendly advice for a cloud engineer explaining what to check and how to fix this.\n\n"
        
        "###PLAN###\n"
        "Now output a JSON object with:\n"
        "  \"actions\": [ { \"type\": \"ssm_command\", \"description\": \"…\", \"commands\": [\"…\"] }, … ],\n"
        "  \"justifications\": [ \"…\", \"…\" ]\n"
        "Do not output anything except these two sections, separated by the markers above.\n"
        "Assistant:"
    )

    try:
        resp = bedrock_client.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "prompt": prompt,
                "max_tokens_to_sample": 400,
                "temperature": 0.5
            })
        )
        raw = resp["body"].read().decode('utf-8')
    except Exception:
        # Fallback if Bedrock is unreachable
        raw = "###ADVICE###\nBedrock unavailable; defaulting to kill top process.\n\n###PLAN###\n{}"

    # ==== 3.a) If Claude returns {"type":"completion","completion":"…"}, pull out "completion" ====
    try:
        maybe_obj = json.loads(raw)
        if isinstance(maybe_obj, dict) and "completion" in maybe_obj:
            raw = maybe_obj["completion"]
    except Exception:
        pass

    # ==== 4) Split on markers ====
    advice_main = ""
    plan_obj = {}
    if "###ADVICE###" in raw and "###PLAN###" in raw:
        parts = raw.split("###PLAN###", 1)
        advice_section = parts[0].replace("###ADVICE###", "").strip()
        plan_section   = parts[1].strip()
        advice_main = advice_section
        try:
            plan_obj = json.loads(plan_section)
        except Exception:
            plan_obj = {}
    else:
        # If markers aren't found, treat raw entire as advice_main
        advice_main = raw
        plan_obj = {}

    # ==== 5) Fallback if plan_obj lacks "actions" ====
    if not plan_obj.get("actions"):
        if top_process_name and top_process_name != "Unknown":
            plan_obj["actions"] = [{
                "type": "ssm_command",
                "description": f"Kill top process '{top_process_name}' to reduce load",
                "commands": [f"sudo pkill -9 {top_process_name}"]
            }]
            plan_obj["justifications"] = [
                f"Killing '{top_process_name}' will immediately reduce CPU/Memory usage."
            ]
        else:
            plan_obj["actions"] = []
            plan_obj["justifications"] = []

    # ==== 6) Execute SSM actions, deferring any reboot ====
    executed_actions = []
    justifications   = plan_obj.get("justifications", [])
    deferred_reboot = None

    for idx, action in enumerate(plan_obj.get("actions", [])):
        if action.get("type") == "ssm_command" and instance_id:
            cmd_list    = action.get("commands", [])
            description = action.get("description", "")
            justification = justifications[idx] if idx < len(justifications) else ""

            # Defer reboot commands
            if any("reboot" in c for c in cmd_list):
                deferred_reboot = {
                    "description": description,
                    "commands": cmd_list,
                    "justification": justification
                }
                continue

            try:
                ssm_resp = ssm_client.send_command(
                    InstanceIds=[instance_id],
                    DocumentName="AWS-RunShellScript",
                    Parameters={"commands": cmd_list},
                    TimeoutSeconds=60
                )
                cmd_id = ssm_resp["Command"]["CommandId"]
                executed_actions.append({
                    "description": description,
                    "commands": cmd_list,
                    "command_id": cmd_id,
                    "status": "SENT",
                    "justification": justification
                })
            except ClientError as e:
                executed_actions.append({
                    "description": description,
                    "commands": cmd_list,
                    "status": f"FAILED: {str(e)}",
                    "justification": justification
                })

    # ==== 7) Validate any AWS doc links in advice_main ====
    urls = re.findall(r'https?://(?:docs\.aws\.amazon\.com|aws\.amazon\.com)/\S+', advice_main)
    valid_urls = []
    for u in urls:
        try:
            r = http.request("HEAD", u, timeout=5.0)
            if 200 <= r.status < 400:
                valid_urls.append(u)
        except:
            pass

    if valid_urls:
        advice_main = advice_main.split(valid_urls[0])[0].strip()
        advice_main += "\n\nAWS Documentation Links:\n" + "\n".join(valid_urls)

    # ==== 8) Build & send SNS email ====
    subject = f"[Alert] EC2 Alarm: {alarm_name}"
    metrics_label   = f"{alarm_type.upper()} Metrics Console"
    processes_label = f"Top 5 {alarm_type.upper()} Processes"

    executed_summary = "\n\n=== Actions Taken by AI ===\n"
    for x in executed_actions:
        executed_summary += (
            f"- Description: {x['description']}\n"
            f"  Commands: {x['commands']}\n"
            f"  Status: {x['status']}\n"
            f"  Justification: {x.get('justification','')}\n\n"
        )
    if deferred_reboot:
        executed_summary += (
            f"- Description: {deferred_reboot['description']}\n"
            f"  Commands: {deferred_reboot['commands']}\n"
            "  Status: PENDING REBOOT\n"
            f"  Justification: {deferred_reboot['justification']}\n\n"
        )
    elif not executed_actions:
        executed_summary += "No automated actions were taken.\n"

    message_body = (
        f"Alarm: {alarm_name}\n"
        f"Time: {timestamp}\n\n"
        f"Resource Details:\n{resource_details}\n\n"
        f"{metrics_label}:\n{metrics_url}\n\n"
        f"{processes_label}:\n{top_processes}\n\n"
        f"Human Advice:\n{advice_main}"
        f"{executed_summary}"
    )

    sns_client.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=subject,
        Message=message_body,
        MessageAttributes={"source": {"DataType":"String","StringValue":"remediation"}}
    )

    # ==== 9) Send to Slack - FIXED SECTION ====
    if SLACK_WEBHOOK_URL:
        try:
            # Create properly formatted Slack message
            slack_text = f":warning: *{alarm_name}* on `{instance_id or 'N/A'}` at {timestamp}"
            
            # Format actions for Slack
            actions_list = []
            for action in executed_actions:
                status = action.get('status', 'SENT')
                actions_list.append(f"• *{action['description']}* (Status: `{status}`)")
            
            if deferred_reboot:
                actions_list.append(f"• *{deferred_reboot['description']}* (Status: `PENDING REBOOT`)")
            
            actions_text = "\n".join(actions_list) if actions_list else "No automated actions taken"
            
            # Truncate long advice text
            max_advice_length = 2000
            if len(advice_main) > max_advice_length:
                advice_text = advice_main[:max_advice_length] + "..."
            else:
                advice_text = advice_main
            
            # Build Slack blocks
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"EC2 {alarm_type.upper()} Alert",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": slack_text
                    }
                },
                {
                    "type": "divider"
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Instance Details:*\n```{resource_details}```"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Top Processes ({alarm_type.upper()}):*\n```{top_processes}```"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*AI Recommendations:*\n{advice_text}"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Automated Actions Taken:*\n{actions_text}"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "View CloudWatch Metrics",
                                "emoji": True
                            },
                            "url": metrics_url,
                            "style": "primary"
                        }
                    ]
                }
            ]
            
            # Add footer with timestamp
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Sent via AWS Lambda at <!date^{int(time.time())}^{{date_num}} {{time_secs}}|{time.ctime()}>"
                    }
                ]
            })
            
            # Build final payload
            slack_payload = {
                "blocks": blocks,
                "text": f"EC2 Alert: {alarm_name} on {instance_id or 'unknown instance'}"
            }
            
            # Send with retry logic
            max_retries = 2
            for attempt in range(max_retries + 1):
                try:
                    response = http.request(
                        "POST",
                        SLACK_WEBHOOK_URL,
                        body=json.dumps(slack_payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"}
                    )
                    if response.status == 200:
                        break
                except Exception as e:
                    print(f"Slack send attempt {attempt} failed: {str(e)}")
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
        except Exception as e:
            print(f"Slack notification failed: {str(e)}")

    # ==== 10) Finally, if we deferred a reboot, send it now ====
    if deferred_reboot and instance_id:
        try:
            _ = ssm_client.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": deferred_reboot["commands"]},
                TimeoutSeconds=60
            )
        except ClientError:
            pass

    return {'statusCode': 200, 'body': json.dumps({'status': 'ok'})}
