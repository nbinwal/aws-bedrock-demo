import json
import boto3
import os
import re
import urllib3

# === AWS & HTTP Clients ===
sns_client     = boto3.client('sns')
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
    # avoid loops
    if record.get('MessageAttributes', {}).get('source', {}).get('StringValue') == 'remediation':
        return {'statusCode': 200, 'body': 'skipped'}

    # parse incoming SNS message
    sns_msg    = json.loads(record['Message'])
    alarm_name = sns_msg.get('AlarmName', 'UnknownAlarm')
    timestamp  = sns_msg.get('StateChangeTime', 'UnknownTime')

    # extract InstanceId if present
    instance_id = None
    trigger = sns_msg.get('Trigger', {}) or sns_msg.get('trigger', {})
    for d in trigger.get('Dimensions', trigger.get('dimensions', [])):
        if (d.get('Name') or d.get('name')) == 'InstanceId':
            instance_id = d.get('Value') or d.get('value')
            break

    # fetch EC2 metadata
    if instance_id:
        resp = ec2_client.describe_instances(InstanceIds=[instance_id])
        inst = resp['Reservations'][0]['Instances'][0]
        name_tag      = next((t['Value'] for t in inst.get('Tags', []) if t['Key']=='Name'), 'N/A')
        inst_type     = inst.get('InstanceType', 'N/A')
        state         = inst.get('State', {}).get('Name', 'N/A')
        az            = inst.get('Placement', {}).get('AvailabilityZone', 'N/A')
        launch_time   = inst.get('LaunchTime').isoformat()
        private_ip    = inst.get('PrivateIpAddress', 'N/A')
        public_ip     = inst.get('PublicIpAddress', 'N/A')

        resource_details = (
            f"Name: {name_tag}\n"
            f"InstanceId: {instance_id}\n"
            f"Type: {inst_type}\n"
            f"State: {state}\n"
            f"AZ: {az}\n"
            f"Launched: {launch_time}\n"
            f"Private IP: {private_ip}\n"
            f"Public IP: {public_ip}"
        )

        cpu_metrics_url = (
            f"https://{AWS_REGION}.console.aws.amazon.com/cloudwatch/home"
            f"?region={AWS_REGION}#resource-health:dashboards/ec2/{instance_id}"
            f"?~(leadingMetric~'*22cpu-utilization*22)"
        )
    else:
        resource_details = "No EC2 instance ID found in alarm."
        cpu_metrics_url = "N/A"

    # build the Bedrock prompt
    prompt = (
        f"Human: A CloudWatch alarm '{alarm_name}' for EC2 instance {instance_id or 'Unknown'} "
        f"fired at {timestamp} due to high CPU usage.\n\n"
        "Provide a detailed troubleshooting and remediation plan for a cloud engineer. "
        "Include common causes, diagnostic steps (e.g., CloudWatch metrics, top/htop), "
        "and remediation actions (e.g., resizing the instance, optimizing code, Auto Scaling). "
        "Then list 3–5 official AWS documentation URLs (one per line) from aws.amazon.com or docs.aws.amazon.com.\n"
        "Assistant:"
    )
    bedrock_payload = json.dumps({
        "prompt": prompt,
        "max_tokens_to_sample": 500,
        "temperature": 0.5
    })

    # invoke Bedrock
    try:
        resp = bedrock_client.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=bedrock_payload
        )
        advice = json.loads(resp['body'].read()).get('completion', '').strip()
    except Exception:
        advice = "Could not retrieve remediation advice from Bedrock."

    # extract and validate AWS doc URLs
    urls = re.findall(r'https?://(?:docs\.aws\.amazon\.com|aws\.amazon\.com)/\S+', advice)
    valid_urls = []
    for u in urls:
        try:
            r = http.request('HEAD', u, timeout=5.0)
            if 200 <= r.status < 400:
                valid_urls.append(u)
        except Exception:
            pass

    # split advice before first URL
    if valid_urls:
        advice_text = advice.split(valid_urls[0])[0].strip()
        advice_text += "\n\nAWS Documentation Links:\n" + "\n".join(valid_urls)
    else:
        advice_text = advice

    # send remediation email via SNS
    subject = f"[Alert] EC2 Alarm: {alarm_name}"
    message = (
        f"Alarm: {alarm_name}\n"
        f"Time: {timestamp}\n\n"
        f"Resource Details:\n{resource_details}\n\n"
        f"CloudWatch CPU Metrics URL:\n{cpu_metrics_url}\n\n"
        f"Remediation Advice:\n{advice_text}"
    )
    sns_client.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=subject,
        Message=message,
        MessageAttributes={"source": {"DataType": "String", "StringValue": "remediation"}}
    )

    # send structured payload to Slack Workflow
    if SLACK_WEBHOOK_URL:
        slack_payload = {
            "alarm_name":       alarm_name,
            "timestamp":        timestamp,
            "resource_details": resource_details,
            "cpu_metrics_url":  cpu_metrics_url,
            "advice":           advice_text
        }
        http.request(
            "POST",
            SLACK_WEBHOOK_URL,
            body=json.dumps(slack_payload).encode('utf-8'),
            headers={"Content-Type": "application/json"}
        )

    return {
        'statusCode': 200,
        'body': json.dumps({'status': 'ok'})
    }
