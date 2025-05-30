import json
import boto3
import os
import re
import time
import urllib3
from botocore.exceptions import ClientError

# === Initialize AWS service clients and HTTP client ===
sns_client     = boto3.client('sns')                 # For sending notifications via SNS
ssm_client     = boto3.client('ssm')                 # For running commands on EC2 instances
bedrock_client = boto3.client('bedrock-runtime', region_name="us-east-1")  # For getting AI-generated advice
ec2_client     = boto3.client('ec2', region_name="us-east-1")             # To get EC2 instance info
http           = urllib3.PoolManager()               # For making HTTP requests (e.g., checking URLs)

# === Configuration variables ===
MODEL_ID          = "anthropic.claude-v2"            # AI model used for generating advice
SNS_TOPIC_ARN     = "arn:aws:sns:us-east-1:657506130129:SmartOpsAlertTopic"  # SNS topic to send emails/alerts
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")  # Slack webhook URL from environment variable
AWS_REGION        = ec2_client.meta.region_name      # AWS region, e.g., us-east-1

def lambda_handler(event, context):
    # Step 1: Get the incoming SNS message from the event trigger
    record = event['Records'][0]['Sns']
    
    # Skip processing if this is a remediation message (to avoid loops)
    if record.get('MessageAttributes', {}).get('source', {}).get('StringValue') == 'remediation':
        return {'statusCode': 200, 'body': 'skipped'}

    # Parse the SNS message which contains CloudWatch alarm details
    sns_msg    = json.loads(record['Message'])
    alarm_name = sns_msg.get('AlarmName', 'UnknownAlarm')           # Name of the alarm
    timestamp  = sns_msg.get('StateChangeTime', 'UnknownTime')      # When the alarm changed state

    # Determine if the alarm is about CPU or Memory usage by checking alarm name text
    alarm_type = 'cpu'
    if 'memory' in alarm_name.lower():
        alarm_type = 'memory'

    # Extract the EC2 Instance ID that the alarm is about from the message
    instance_id = None
    trigger = sns_msg.get('Trigger', {}) or sns_msg.get('trigger', {})
    for d in trigger.get('Dimensions', trigger.get('dimensions', [])):
        if (d.get('Name') or d.get('name')) == 'InstanceId':
            instance_id = d.get('Value') or d.get('value')
            break

    # Step 2: Fetch EC2 instance metadata like name, type, IPs, etc.
    if instance_id:
        resp = ec2_client.describe_instances(InstanceIds=[instance_id])
        inst = resp['Reservations'][0]['Instances'][0]
        # Find instance Name tag, if any
        name_tag   = next((t['Value'] for t in inst.get('Tags', []) if t['Key']=='Name'), 'N/A')
        inst_type  = inst.get('InstanceType', 'N/A')
        state      = inst.get('State', {}).get('Name', 'N/A')
        az         = inst.get('Placement', {}).get('AvailabilityZone', 'N/A')
        launch_iso = inst.get('LaunchTime').isoformat()
        priv_ip    = inst.get('PrivateIpAddress', 'N/A')
        pub_ip     = inst.get('PublicIpAddress', 'N/A')

        # Format instance details into a readable string
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

        # Build a CloudWatch URL where you can see the metrics related to this alarm & instance
        metrics_url = (
            f"https://{AWS_REGION}.console.aws.amazon.com/cloudwatch/home"
            f"?region={AWS_REGION}#resource-health:dashboards/ec2/{instance_id}?"
            f"~(leadingMetric~'*22{alarm_type}-utilization*22)"
        )
    else:
        resource_details = "No EC2 instance ID found in alarm."
        metrics_url = "N/A"

    # Step 3: Fetch the top 5 processes consuming CPU or Memory from the instance via SSM
    top_processes = "Not available"
    top_process_name = "Unknown"
    if instance_id:
        try:
            # Prepare the shell command to list processes sorted by CPU or Memory usage
            if alarm_type == "cpu":
                shell_cmd = "ps -eo pid,comm,%cpu --sort=-%cpu | head -n6"
            else:
                shell_cmd = "ps -eo pid,comm,%mem --sort=-%mem | head -n6"

            # Send the command to the EC2 instance using SSM (AWS Systems Manager)
            cmd = ssm_client.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [shell_cmd]},
                TimeoutSeconds=30
            )
            cmd_id = cmd["Command"]["CommandId"]

            # Wait and retry to get the command output until success or timeout
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
                    # Sometimes invocation info may not be ready yet
                    if error.response['Error']['Code'] == 'InvocationDoesNotExist':
                        time.sleep(retry_delay)
                        continue
                    else:
                        raise
                time.sleep(retry_delay)

            # If command succeeded, parse output to get top processes
            if inv and inv["Status"] == "Success":
                top_processes = inv["StandardOutputContent"].strip()
                lines = top_processes.splitlines()
                if len(lines) >= 2:
                    first_proc_line = lines[1].strip()  # first process after header
                    parts = first_proc_line.split()
                    if len(parts) >= 2:
                        top_process_name = parts[1]     # process name
            elif inv:
                top_processes = f"SSM command status: {inv['Status']}"
            else:
                top_processes = "SSM command did not complete"
        except Exception as e:
            top_processes = f"Error fetching processes via SSM: {e}"

    # Step 4: Ask AWS Bedrock AI for advice on troubleshooting & remediation
    metric_unit = "%CPU" if alarm_type == "cpu" else "%MEM"
    prompt = (
        f"Human: A CloudWatch alarm '{alarm_name}' for EC2 instance {instance_id or 'Unknown'} "
        f"fired at {timestamp} due to high {alarm_type.upper()} usage.\n\n"
        f"The top process consuming {metric_unit} is: '{top_process_name}'.\n"
        f"Here are the top 5 processes by {metric_unit}:\n{top_processes}\n\n"
        f"Please analyze why this process might cause high {alarm_type.upper()} and provide a concise troubleshooting and remediation plan for a cloud engineer. "
        f"Then list 3–5 official AWS documentation URLs (one per line).\n"
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
        # Extract the advice text from the AI response
        advice = json.loads(resp["body"].read()).get("completion", "").strip()
    except Exception:
        advice = "Could not retrieve remediation advice from Bedrock."

    # Step 5: Validate AWS documentation URLs from the advice to ensure they are reachable
    urls = re.findall(r'https?://(?:docs\.aws\.amazon\.com|aws\.amazon\.com)/\S+', advice)
    valid_urls = []
    for u in urls:
        try:
            r = http.request("HEAD", u, timeout=5.0)
            if 200 <= r.status < 400:
                valid_urls.append(u)
        except:
            pass

    # Append valid URLs neatly after advice text, or just show advice if none found
    if valid_urls:
        advice_text = advice.split(valid_urls[0])[0].strip()
        advice_text += "\n\nAWS Documentation Links:\n" + "\n".join(valid_urls)
    else:
        advice_text = advice

    # Step 6: Send an email via SNS with all the details and advice
    subject = f"[Alert] EC2 Alarm: {alarm_name}"
    
    # Labels for sections depending on alarm type (CPU or Memory)
    metrics_label = f"{alarm_type.upper()} Metrics Console"
    processes_label = f"Top 5 {alarm_type.upper()} Processes"
    
    # Construct the full message body for the email
    message = (
        f"Alarm: {alarm_name}\n"
        f"Time: {timestamp}\n\n"
        f"Resource Details:\n{resource_details}\n\n"
        f"{metrics_label}:\n{metrics_url}\n\n"
        f"{processes_label}:\n{top_processes}\n\n"
        f"Remediation Advice:\n{advice_text}"
    )
    sns_client.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=subject,
        Message=message,
        MessageAttributes={"source": {"DataType":"String","StringValue":"remediation"}}
    )

    # Step 7: Also send the information to a Slack channel if webhook URL is set
    if SLACK_WEBHOOK_URL:
        # Use more friendly labels for Slack display
        metrics_type_display = "CPU" if alarm_type == "cpu" else "Memory"
        
        slack_payload = {
            "alarm_name": alarm_name,
            "timestamp": timestamp,
            "resource_details": resource_details,
            "metrics_type": metrics_type_display,
            "metrics_url": metrics_url,
            "top_processes": top_processes,
            "advice": advice_text
        }
        # Post the message as JSON to Slack webhook
        http.request(
            "POST",
            SLACK_WEBHOOK_URL,
            body=json.dumps(slack_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )

    # Return success response for Lambda execution
    return {'statusCode': 200, 'body': json.dumps({'status': 'ok'})}
