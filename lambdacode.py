import json
import boto3
import os
import re
import time
import urllib3
from botocore.exceptions import ClientError

# === AWS & HTTP Clients ===
# Initialize clients for SNS (Simple Notification Service), SSM (System Manager),
# Bedrock (for AI), EC2 (to fetch instance metadata), and HTTP requests
sns_client     = boto3.client('sns')
ssm_client     = boto3.client('ssm')
bedrock_client = boto3.client('bedrock-runtime', region_name="us-east-1")
ec2_client     = boto3.client('ec2', region_name="us-east-1")
http           = urllib3.PoolManager()

# === Configuration ===
# MODEL_ID: which Bedrock model to use for generating advice and plan
MODEL_ID       = "anthropic.claude-v2"

# SNS_TOPIC_ARN: the SNS topic where CloudWatch Alarms publish their notifications
SNS_TOPIC_ARN  = "arn:aws:sns:us-east-1:657506130129:SmartOpsAlertTopic"

# Slack webhook URL must be set as an environment variable in the Lambda configuration
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

# AWS_REGION: region in which this EC2 instance is running; used for building console URLs
AWS_REGION     = ec2_client.meta.region_name


def is_json(s: str) -> bool:
    """
    Check if the provided string is valid JSON.
    Returns True if json.loads(s) succeeds, otherwise False.
    """
    try:
        json.loads(s)
        return True
    except:
        return False


def lambda_handler(event, context):
    """
    Main Lambda handler. This function branches into two paths:
    
    1) JSON Path (CloudWatch → SNS in JSON):
       - Triggered when CloudWatch Alarm transitions from OK->ALARM.
       - Gathers EC2 instance metadata and top processes using SSM.
       - Calls Bedrock to generate both human advice and a JSON plan of automated actions.
       - Executes any “kill” or other SSM commands suggested by Bedrock (defers “reboot” until after notifications).
       - Sends exactly one Slack notification (with nine fields).
       - Publishes a single plaintext message back to the same SNS topic with MessageAttributes {"source":"remediation","slack":"true"},
         so that Outlook (or any email subscriber) gets exactly one email.

    2) Plaintext Path (SNS re‐invokes Lambda with the plaintext we just published):
       - Recognizes plaintext messages beginning with "Alarm:". 
       - Skips Slack (Outlook/email path has already delivered).
       - Returns immediately (no loop back into JSON path).

    3) If MessageAttributes.source == "remediation", skip immediately (prevents infinite loop).
    """
    record  = event['Records'][0]['Sns']
    raw_msg = record.get('Message', "")

    # === 0) Loop prevention: if this message has source="remediation", we do not process again ===
    source_attr = record.get('MessageAttributes', {}) \
                        .get('source', {}) \
                        .get('StringValue')
    if source_attr == "remediation":
        # We have already processed this alarm and sent notifications, so do nothing
        return {'statusCode': 200, 'body': 'skipped'}

    # === 1) JSON Path: if the message is valid JSON, it's a CloudWatch Alarm payload ===
    if is_json(raw_msg):
        sns_msg = json.loads(raw_msg)

        # Extract old and new state: we only want to act when the alarm just entered ALARM
        old_state = sns_msg.get("OldStateValue", "")
        new_state = sns_msg.get("NewStateValue", "")
        if not (old_state != "ALARM" and new_state == "ALARM"):
            # If this is not the transition we care about, do nothing
            return {'statusCode': 200, 'body': 'skipped non-transition'}

        # Pull out the alarm name and timestamp of the state change
        alarm_name = sns_msg.get('AlarmName', 'UnknownAlarm')
        timestamp  = sns_msg.get('StateChangeTime', 'UnknownTime')

        # Determine if this is a CPU or Memory alarm based on its name
        alarm_type = 'cpu'
        if 'memory' in alarm_name.lower():
            alarm_type = 'memory'

        # Extract the EC2 instance ID from the SNS JSON (under Trigger → Dimensions)
        instance_id = None
        trigger = sns_msg.get('Trigger', {}) or sns_msg.get('trigger', {})
        for d in trigger.get('Dimensions', trigger.get('dimensions', [])):
            dim_name = d.get('Name') or d.get('name')
            if dim_name == 'InstanceId':
                instance_id = d.get('Value') or d.get('value')
                break

        # ==== A) Fetch EC2 metadata for that instance ID ====
        if instance_id:
            try:
                resp = ec2_client.describe_instances(InstanceIds=[instance_id])
                inst = resp['Reservations'][0]['Instances'][0]
            except:
                inst = {}

            # Get human-friendly Name tag (if exists), instance type, state, AZ, launch time, IPs
            name_tag   = next((t['Value'] for t in inst.get('Tags', []) if t['Key']=='Name'), 'N/A')
            inst_type  = inst.get('InstanceType', 'N/A')
            state      = inst.get('State', {}).get('Name', 'N/A')
            az         = inst.get('Placement', {}).get('AvailabilityZone', 'N/A')
            launch_iso = inst.get('LaunchTime').isoformat() if inst.get('LaunchTime') else 'N/A'
            priv_ip    = inst.get('PrivateIpAddress', 'N/A')
            pub_ip     = inst.get('PublicIpAddress', 'N/A')

            # Build a multi-line string for “Resource Details”
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

            # Build the CloudWatch Metrics Console URL (with dynamic Region + Alarm type)
            metrics_url = (
                f"https://{AWS_REGION}.console.aws.amazon.com/cloudwatch/home"
                f"?region={AWS_REGION}#resource-health:dashboards/ec2/{instance_id}?~"
                f"(leadingMetric~'*22{alarm_type}-utilization*22)"
            )
        else:
            # If no instance ID found, fill placeholders
            resource_details = "No EC2 instance ID found in alarm."
            metrics_url      = "N/A"

        # ==== B) Fetch top 5 processes on the instance using SSM RunCommand ====
        top_processes    = "Not available"
        top_process_name = "Unknown"
        if instance_id:
            try:
                # Choose the right “ps” command based on CPU vs Memory alarm
                if alarm_type == "cpu":
                    shell_cmd = "ps -eo pid,comm,%cpu --sort=-%cpu | head -n6"
                else:
                    shell_cmd = "ps -eo pid,comm,%mem --sort=-%mem | head -n6"

                # Send SSM RunCommand to capture top processes
                cmd = ssm_client.send_command(
                    InstanceIds=[instance_id],
                    DocumentName="AWS-RunShellScript",
                    Parameters={"commands": [shell_cmd]},
                    TimeoutSeconds=30
                )
                cmd_id = cmd["Command"]["CommandId"]

                # Poll until the command finishes (up to 8 retries with 2s delay)
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

                # If the command succeeded, grab its output
                if inv and inv["Status"] == "Success":
                    top_processes = inv["StandardOutputContent"].strip()
                    # Extract the first real process line to know the top process name
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

        # ==== C) Ask Bedrock for advice + plan (with explicit markers) ====
        metric_unit = "%CPU" if alarm_type == "cpu" else "%MEM"
        prompt = (
            f"Human: A CloudWatch alarm '{alarm_name}' for EC2 instance {instance_id or 'Unknown'} "
            f"fired at {timestamp} due to high {alarm_type.upper()} usage.\n\n"
            f"The top process consuming {metric_unit} is: '{top_process_name}'.\n"
            f"Here are the top 5 processes by {metric_unit}:\n{top_processes}\n\n"

            "###ADVICE###\n"
            "Please provide a clear, concise paragraph of human-friendly advice "
            "for a cloud engineer explaining what to check and how to fix this.\n\n"

            "###PLAN###\n"
            "Now output a JSON object with:\n"
            "  \"actions\": [ { \"type\": \"ssm_command\", \"description\": \"…\", \"commands\": [\"…\"] }, … ],\n"
            "  \"justifications\": [ \"…\", \"…\" ]\n"
            "Do not output anything except those two sections, separated by the markers above.\n"
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
            # If Bedrock is unavailable, default to a plan that kills the top process
            raw = "###ADVICE###\nBedrock unavailable; defaulting to kill top process.\n\n###PLAN###\n{}"

        # === C1) If Bedrock returned {"type":"completion","completion":…}, extract the "completion" field ===
        try:
            maybe_obj = json.loads(raw)
            if isinstance(maybe_obj, dict) and "completion" in maybe_obj:
                raw = maybe_obj["completion"]
        except:
            pass

        # ==== D) Split raw into advice_main (human paragraph) vs plan_obj (JSON plan) ====
        advice_main = ""
        plan_obj    = {}
        if "###ADVICE###" in raw and "###PLAN###" in raw:
            parts = raw.split("###PLAN###", 1)
            advice_section = parts[0].replace("###ADVICE###", "").strip()
            plan_section   = parts[1].strip()
            advice_main    = advice_section
            try:
                plan_obj = json.loads(plan_section)
            except:
                plan_obj = {}
        else:
            # If markers not found, treat entire raw as advice_main
            advice_main = raw
            plan_obj    = {}

        # ==== E) If plan_obj lacks "actions", fallback to killing top process ====
        if not plan_obj.get("actions"):
            if top_process_name and top_process_name != "Unknown":
                plan_obj["actions"] = [{
                    "type":        "ssm_command",
                    "description": f"Kill top process '{top_process_name}' to reduce load",
                    "commands":    [f"sudo pkill -9 {top_process_name}"]
                }]
                plan_obj["justifications"] = [
                    f"Killing '{top_process_name}' will immediately reduce CPU/Memory usage."
                ]
            else:
                plan_obj["actions"]        = []
                plan_obj["justifications"] = []

        # ==== F) Execute SSM actions from plan_obj; defer any "reboot" commands ====
        executed_actions = []
        justifications   = plan_obj.get("justifications", [])
        deferred_reboot  = None

        for idx, action in enumerate(plan_obj.get("actions", [])):
            if action.get("type") == "ssm_command" and instance_id:
                cmd_list      = action.get("commands", [])
                description   = action.get("description", "")
                justification = justifications[idx] if idx < len(justifications) else ""

                # If any command contains "reboot", save it to deferred_reboot and skip execution now
                if any("reboot" in c.lower() for c in cmd_list):
                    deferred_reboot = {
                        "description":   description,
                        "commands":      cmd_list,
                        "justification": justification
                    }
                    continue

                # Convert placeholders like "kill <stress_pid>" or "grep stress" into a concrete pkill
                final_cmds = []
                for c in cmd_list:
                    if "<stress_pid>" in c or "grep stress" in c:
                        final_cmds = [f"sudo pkill -9 {top_process_name}"]
                        break
                if not final_cmds:
                    final_cmds = cmd_list

                try:
                    ssm_resp = ssm_client.send_command(
                        InstanceIds=[instance_id],
                        DocumentName="AWS-RunShellScript",
                        Parameters={"commands": final_cmds},
                        TimeoutSeconds=60
                    )
                    cmd_id = ssm_resp["Command"]["CommandId"]
                    executed_actions.append({
                        "description":   description,
                        "commands":      final_cmds,
                        "command_id":    cmd_id,
                        "status":        "SENT",
                        "justification": justification
                    })
                except ClientError as e:
                    executed_actions.append({
                        "description":   description,
                        "commands":      final_cmds,
                        "status":        f"FAILED: {str(e)}",
                        "justification": justification
                    })

        # ==== G) Scrub any AWS documentation links from advice_main and append at bottom ==== 
        urls       = re.findall(r'https?://(?:docs\.aws\.amazon\.com|aws\.amazon\.com)/\S+', advice_main)
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

        # ==== H) SEND SLACK (exactly nine fields) – do this first before SNS publish ====
        if SLACK_WEBHOOK_URL:
            # Format actions_taken as a multi-line string
            if executed_actions:
                actions_taken_lines = []
                for x in executed_actions:
                    actions_taken_lines.append(
                        f"- Description: {x['description']}\n"
                        f"  Commands: {x['commands']}\n"
                        f"  Status: {x['status']}\n"
                        f"  Justification: {x.get('justification','')}"
                    )
                if deferred_reboot:
                    actions_taken_lines.append(
                        f"- Description: {deferred_reboot['description']}\n"
                        f"  Commands: {deferred_reboot['commands']}\n"
                        "  Status: PENDING REBOOT\n"
                        f"  Justification: {deferred_reboot['justification']}"
                    )
                actions_taken_str = "\n\n".join(actions_taken_lines)
            else:
                actions_taken_str = "No automated actions were taken."

            # Build the Slack JSON payload with exactly nine keys
            slack_payload = {
                "alarm_name":       alarm_name,
                "timestamp":        timestamp,
                "resource_details": resource_details,
                "metrics_type":     alarm_type.upper(),
                "metrics_url":      metrics_url,
                "processes_type":   alarm_type.upper(),
                "top_processes":    top_processes,
                "advice":           advice_main,
                "actions_taken":    actions_taken_str
            }

            # Send Slack message with up to 2 retries
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
                except:
                    time.sleep(2 ** attempt)

        # ==== I) Publish PLAINTEXT to SNS so Outlook / email subscribers get exactly one email ====
        subject         = f"[Alert] EC2 Alarm: {alarm_name}"
        metrics_label   = f"{alarm_type.upper()} Metrics Console"
        processes_label = f"Top 5 {alarm_type.upper()} Processes"

        # Build the “Actions Taken” section for the plaintext email
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

        # Build the full plaintext email body
        message_body = (
            f"Alarm: {alarm_name}\n"
            f"Time: {timestamp}\n\n"
            f"Resource Details:\n{resource_details}\n\n"
            f"{metrics_label}:\n{metrics_url}\n\n"
            f"{processes_label}:\n{top_processes}\n\n"
            f"General Remediation Advice:\n{advice_main}"
            f"{executed_summary}"
        )

        # Publish back to the same SNS topic with MessageAttributes to identify this as our “remediation” publication
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message_body,
            MessageAttributes={
                "source": {"DataType":"String", "StringValue":"remediation"},
                # "slack" attribute is optional because in plaintext path we skip Slack entirely
                "slack":  {"DataType":"String", "StringValue":"true"}
            }
        )

        # Return success
        return {'statusCode': 200, 'body': json.dumps({'status': 'ok'})}

    # === 2) Plaintext Path: if SNS message starts with “Alarm:” we skip Slack ===
    if raw_msg.startswith("Alarm:"):
        # Since Outlook/email already delivered this plaintext, we do nothing here
        return {'statusCode': 200, 'body': 'skipped plaintext path (no Slack)'}

    # === 3) Otherwise, skip unknown formats ===
    return {'statusCode': 200, 'body': 'skipped unknown format'}
