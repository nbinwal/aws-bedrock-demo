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
    record = event['Records'][0]['Sns']
    if record.get('MessageAttributes', {}).get('source', {}).get('StringValue') == 'remediation':
        return {'statusCode': 200, 'body': 'skipped'}

    sns_msg    = json.loads(record['Message'])
    alarm_name = sns_msg.get('AlarmName', 'UnknownAlarm')
    timestamp  = sns_msg.get('StateChangeTime', 'UnknownTime')

    # Extract InstanceId
    instance_id = None
    trigger = sns_msg.get('Trigger', {}) or sns_msg.get('trigger', {})
    for d in trigger.get('Dimensions', trigger.get('dimensions', [])):
        if (d.get('Name') or d.get('name')) == 'InstanceId':
            instance_id = d.get('Value') or d.get('value')
            break

    # ==== 1) Fetch EC2 metadata ====
    if instance_id:
        resp = ec2_client.describe_instances(InstanceIds=[instance_id])
        inst = resp['Reservations'][0]['Instances'][0]
        name_tag   = next((t['Value'] for t in inst.get('Tags', []) if t['Key']=='Name'), 'N/A')
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

        cpu_metrics_url = (
            f"https://{AWS_REGION}.console.aws.amazon.com/cloudwatch/home"
            f"?region={AWS_REGION}#resource-health:dashboards/ec2/{instance_id}"
            f"?~(leadingMetric~'*22cpu-utilization*22)"
        )
    else:
        resource_details = "No EC2 instance ID found in alarm."
        cpu_metrics_url = "N/A"

    # ==== 2) Fetch top 5 CPU processes via SSM ====
    top_processes = "Not available"
    top_process_name = "Unknown"
    if instance_id:
        try:
            cmd = ssm_client.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": ["ps -eo pid,comm,%cpu --sort=-%cpu | head -n6"]},
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
                        if attempt < max_retries - 1:
                            time.sleep(retry_delay)
                            continue
                        else:
                            top_processes = "SSM command timed out: Invocation not available after retries"
                            break
                    else:
                        raise
                time.sleep(retry_delay)

            if inv and inv["Status"] == "Success":
                top_processes = inv["StandardOutputContent"].strip()

                # Extract top process name from second line
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

    # ==== 3) Ask Bedrock for advice ====
    prompt = (
        f"Human: A CloudWatch alarm '{alarm_name}' for EC2 instance {instance_id or 'Unknown'} "
        f"fired at {timestamp} due to high CPU usage.\n\n"
        f"The top process consuming CPU is: '{top_process_name}'.\n"
        f"Here are the top 5 CPU-consuming processes:\n{top_processes}\n\n"
        "Please analyze why this process might cause high CPU and provide a concise troubleshooting and remediation plan for a cloud engineer. "
        "Then list 3–5 official AWS documentation URLs (one per line).\n"
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
        advice = json.loads(resp["body"].read()).get("completion", "").strip()
    except Exception:
        advice = "Could not retrieve remediation advice from Bedrock."

    # ==== 4) Validate AWS doc links ====
    urls = re.findall(r'https?://(?:docs\.aws\.amazon\.com|aws\.amazon\.com)/\S+', advice)
    valid_urls = []
    for u in urls:
        try:
            r = http.request("HEAD", u, timeout=5.0)
            if 200 <= r.status < 400:
                valid_urls.append(u)
        except:
            pass

    if valid_urls:
        advice_text = advice.split(valid_urls[0])[0].strip()
        advice_text += "\n\nAWS Documentation Links:\n" + "\n".join(valid_urls)
    else:
        advice_text = advice

    # ==== 5) Send SNS email ====
    subject = f"[Alert] EC2 Alarm: {alarm_name}"
    message = (
        f"Alarm: {alarm_name}\n"
        f"Time: {timestamp}\n\n"
        f"Resource Details:\n{resource_details}\n\n"
        f"CloudWatch CPU Metrics URL:\n{cpu_metrics_url}\n\n"
        f"Top CPU Processes:\n{top_processes}\n\n"
        f"Remediation Advice:\n{advice_text}"
    )
    sns_client.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=subject,
        Message=message,
        MessageAttributes={"source": {"DataType":"String","StringValue":"remediation"}}
    )

    # ==== 6) Send to Slack ====
    if SLACK_WEBHOOK_URL:
        slack_payload = {
            "alarm_name":       alarm_name,
            "timestamp":        timestamp,
            "resource_details": resource_details,
            "cpu_metrics_url":  cpu_metrics_url,
            "top_processes":    top_processes,
            "advice":           advice_text
        }
        http.request(
            "POST",
            SLACK_WEBHOOK_URL,
            body=json.dumps(slack_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )

    return {'statusCode': 200, 'body': json.dumps({'status': 'ok'})}
