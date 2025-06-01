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
ec2_client     = boto3.client('ec2',       region_name="us-east-1")
http           = urllib3.PoolManager()

# === Configuration ===
MODEL_ID          = "anthropic.claude-v2"
# The SNS topic ARN to which your CloudWatch Alarm is publishing
SNS_TOPIC_ARN     = "arn:aws:sns:us-east-1:657506130129:SmartOpsAlertTopic"
# In Lambda console → Configuration → Environment Variables, set:
# SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/XXX/YYYY/ZZZZ"
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
AWS_REGION        = ec2_client.meta.region_name

def is_json(s: str) -> bool:
    """Return True if the string s is valid JSON."""
    try:
        json.loads(s)
        return True
    except:
        return False

def lambda_handler(event, context):
    """
    1) JSON Path: Triggered by CloudWatch → SNS (JSON).  
       • Runs only when OldStateValue != "ALARM" and NewStateValue == "ALARM".  
       • Gathers EC2 metadata + top processes (SSM), calls Bedrock, executes any auto‐kill via SSM, and defers reboot.  
       • Sends a single plaintext SNS (source=remediation) so Outlook receives one email.  
       • Sends exactly nine fields to Slack (JSON).

    2) Plaintext Path: Triggered if Lambda receives a plaintext “Alarm:” message  
       • Does *not* re‐publish it to SNS (so the loop stops).  
       • Extracts the nine fields from that plaintext, and sends exactly those nine fields to Slack.  
       • Returns.  

    3) If MessageAttributes.source == "remediation", skip immediately.
    """

    record  = event['Records'][0]['Sns']
    raw_msg = record.get('Message', "")

    # === 0) Prevent recursion if SNS message is our own "source=remediation" publish ===
    source_attr = record.get('MessageAttributes', {}) \
                        .get('source', {}) \
                        .get('StringValue')
    if source_attr == "remediation":
        return {'statusCode': 200, 'body': 'skipped'}

    # === 1) If the SNS message is valid JSON → JSON path ===
    if is_json(raw_msg):
        sns_msg = json.loads(raw_msg)

        # Extract the alarm’s old & new state
        old_state = sns_msg.get("OldStateValue", "")
        new_state = sns_msg.get("NewStateValue", "")

        # Only proceed if it just turned INTO ALARM (was not already ALARM)
        if not (old_state != "ALARM" and new_state == "ALARM"):
            return {'statusCode': 200, 'body': 'skipped non-transition'}

        alarm_name = sns_msg.get('AlarmName', 'UnknownAlarm')
        timestamp  = sns_msg.get('StateChangeTime', 'UnknownTime')

        # Determine CPU vs Memory alarm
        alarm_type = 'cpu'
        if 'memory' in alarm_name.lower():
            alarm_type = 'memory'

        # Extract EC2 InstanceId from the SNS JSON
        instance_id = None
        trigger     = sns_msg.get('Trigger', {}) or sns_msg.get('trigger', {})
        for d in trigger.get('Dimensions', trigger.get('dimensions', [])):
            dim_name = d.get('Name') or d.get('name')
            if dim_name == 'InstanceId':
                instance_id = d.get('Value') or d.get('value')
                break

        # ==== A) Fetch EC2 metadata ====
        if instance_id:
            try:
                resp = ec2_client.describe_instances(InstanceIds=[instance_id])
                inst = resp['Reservations'][0]['Instances'][0]
            except:
                inst = {}

            name_tag   = next((t['Value'] for t in inst.get('Tags', []) if t['Key']=='Name'), 'N/A')
            inst_type  = inst.get('InstanceType', 'N/A')
            state      = inst.get('State', {}).get('Name', 'N/A')
            az         = inst.get('Placement', {}).get('AvailabilityZone', 'N/A')
            launch_iso = inst.get('LaunchTime').isoformat() if inst.get('LaunchTime') else 'N/A'
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
            metrics_url      = "N/A"

        # ==== B) Fetch top 5 processes via SSM ====
        top_processes    = "Not available"
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

        # ==== C) Ask Bedrock with explicit markers ====
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
            raw = "###ADVICE###\nBedrock unavailable; defaulting to kill top process.\n\n###PLAN###\n{}"

        # ==== C1) If Claude returned {"type":"completion","completion":"…"} ====
        try:
            maybe_obj = json.loads(raw)
            if isinstance(maybe_obj, dict) and "completion" in maybe_obj:
                raw = maybe_obj["completion"]
        except:
            pass

        # ==== D) Split that “raw” into advice_main vs. plan_obj ====
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
            advice_main = raw
            plan_obj    = {}

        # ==== E) Fallback if plan_obj lacks "actions" ====
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

        # ==== F) Execute SSM actions, deferring any reboot ====
        executed_actions = []
        justifications   = plan_obj.get("justifications", [])
        deferred_reboot  = None

        for idx, action in enumerate(plan_obj.get("actions", [])):
            if action.get("type") == "ssm_command" and instance_id:
                cmd_list      = action.get("commands", [])
                description   = action.get("description", "")
                justification = justifications[idx] if idx < len(justifications) else ""

                # If any command contains “reboot”, defer it
                if any("reboot" in c.lower() for c in cmd_list):
                    deferred_reboot = {
                        "description":   description,
                        "commands":      cmd_list,
                        "justification": justification
                    }
                    continue

                # If user used placeholders like “kill <stress_pid>” or “grep stress”, convert to pkill:
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

        # ==== G) Scrub AWS doc links out of advice_main and append at bottom ====
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

        # ==== H) Build & send Plaintext to SNS for Outlook (with source=remediation) ====
        subject         = f"[Alert] EC2 Alarm: {alarm_name}"
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

        # **Publish plaintext back to SNS so Outlook gets the email once.**
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message_body,
            MessageAttributes={"source": {"DataType":"String", "StringValue":"remediation"}}
        )

        # ==== I) Send to Slack (exactly nine fields) ====
        if SLACK_WEBHOOK_URL:
            # Build a multi-line string for “actions_taken”
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

        return {'statusCode': 200, 'body': json.dumps({'status': 'ok'})}

    # === 2) If the SNS message is plaintext starting with “Alarm:” → plaintext path ===
    if raw_msg.startswith("Alarm:"):
        # Helper: grab a block of lines after a label until the next label or end-of-text
        def extract_block(label):
            pattern = rf"{label}:\n(.*?)(?=\n[A-Z][^:]+:|\Z)"
            m = re.search(pattern, raw_msg, re.DOTALL)
            return m.group(1).strip() if m else "Not available"

        alarm_name       = re.search(r"Alarm:\s*(.+)", raw_msg).group(1).strip()
        timestamp        = re.search(r"Time:\s*(.+)", raw_msg).group(1).strip()
        resource_details = extract_block("Resource Details")
        metrics_url      = extract_block("CPU Metrics Console") or extract_block("Memory Metrics Console")
        top_processes    = extract_block("Top 5 CPU Processes") or extract_block("Top 5 Memory Processes")
        advice_text      = extract_block("Human Advice")
        actions_taken    = extract_block("=== Actions Taken by AI ===")

        # Send exactly those nine variables to Slack—no SNS republish
        if SLACK_WEBHOOK_URL:
            processes_type = "CPU" if "Top 5 CPU Processes" in raw_msg else "Memory"
            slack_payload = {
                "alarm_name":       alarm_name,
                "timestamp":        timestamp,
                "resource_details": resource_details,
                "metrics_type":     processes_type,
                "metrics_url":      metrics_url,
                "processes_type":   processes_type,
                "top_processes":    top_processes,
                "advice":           advice_text,
                "actions_taken":    actions_taken
            }
            try:
                _ = http.request(
                    "POST",
                    SLACK_WEBHOOK_URL,
                    body=json.dumps(slack_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
            except:
                pass

        # **NO SNS re-publish here** (prevents loops). Outlook already got the original CloudWatch email.
        return {'statusCode': 200, 'body': 'Slack sent from plaintext path'}

    # === 3) Otherwise skip unknown format ===
    return {'statusCode': 200, 'body': 'skipped unknown format'}
