# aws-bedrock-demo
---

## How Does Amazon Bedrock Provide Remediation?

Foundation models (like Anthropic Claude) are trained on massive technical corpora (docs, blogs, forums). When you send a prompt, they generate responses based on patterns they've learned.

This POC:

- Monitors an EC2 instance's CPU utilization using CloudWatch.
- Triggers a Lambda function when CPU usage exceeds 75%.
- Invokes Amazon Bedrock to get remediation steps.

---

## Step-by-Step Implementation

### Step 1: Launch & Prepare EC2

1. **Open EC2 Console**: [https://console.aws.amazon.com/ec2](https://console.aws.amazon.com/ec2)
2. **Launch Instance**:

   * Click **Launch Instance**
   * **Name**: `POC-EC2`
   * **AMI**: Amazon Linux 2
   * **Type**: t2.micro
   * **Key pair**: Create new → `POCKey` → Download `.pem`
   * **Security group**: Allow SSH (port 22) from your IP
   * **Launch**
3. **Connect via SSH**:

   ```bash
   chmod 400 ~/Downloads/POCKey.pem   # Protect key
   ssh -i ~/Downloads/POCKey.pem ec2-user@<PUBLIC_IP>  # Connect
   ```
4. **Install CloudWatch Agent**:

   ```bash
   sudo yum update -y                          # Update OS packages
   sudo yum install -y amazon-cloudwatch-agent # Install agent

   # Run config wizard, press Enter to accept defaults, ensure CPU is enabled
   sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-config-wizard

   sudo systemctl start amazon-cloudwatch-agent # Start agent
   sudo systemctl status amazon-cloudwatch-agent# Verify it's running
   ```

---

### Step 2: Create CloudWatch Alarm

1. **Open CloudWatch**: [https://console.aws.amazon.com/cloudwatch](https://console.aws.amazon.com/cloudwatch)
2. **Create Alarm**:

   * **Alarms** → **Create alarm**
   * **Select metric** → **EC2** → **Per-Instance Metrics** → **CPUUtilization** → **Select metric**
   * **Threshold**: CPU ≥ 75%, Period = 1 minute
   * **Alarm actions**: Invoke Lambda → **Create new function** named `EC2AlertProcessor` (we'll configure it later)
   * **Name**: `POC-HighCPU`
   * **Create alarm**

---

### Step 3: Create IAM Role for Lambda

1. **Open IAM**: [https://console.aws.amazon.com/iam/](https://console.aws.amazon.com/iam/)
2. **Create Role**:

   * **Roles** → **Create role**
   * **Trusted entity**: AWS service → **Lambda** → **Next**
   * **Attach policies**:

     * CloudWatchLogsFullAccess
     * AmazonBedrockInvokeFullAccess
     * AWSLambdaBasicExecutionRole
   * **Name**: `LambdaBedrockExecutionRole` → **Create role**

---

### Step 4: Configure Lambda Function

1. **Open Lambda**: [https://console.aws.amazon.com/lambda](https://console.aws.amazon.com/lambda)
2. **Select Function**: `EC2AlertProcessor` (created by Alarm)
3. **Assign Role**:

   * Configuration → Permissions → Edit → Use existing role → `LambdaBedrockExecutionRole`
4. **Ensure Bedrock Policy**:

   * In IAM, attach `AmazonBedrockInvokeFullAccess` to the role if missing
5. **Add/Replace Code**:
```python
import json             # Built-in module to convert between JSON text and Python dictionaries
import boto3            # AWS SDK for Python, lets us call AWS services like SNS, EC2, Bedrock
import os               # Provides access to environment variables (for secrets like Slack URL)
import urllib3          # Lightweight HTTP client used to send data to Slack

# === SETUP CONNECTIONS TO AWS SERVICES AND SLACK ===

# Create a client object to talk to Amazon SNS (Simple Notification Service).
# We use SNS to send out email alerts.
sns_client     = boto3.client('sns')

# Create a client for Amazon Bedrock Runtime.
# Bedrock is an AI service; we'll send it a prompt and get back remediation advice.
bedrock_client = boto3.client('bedrock-runtime', region_name="us-east-1")

# Create a client for Amazon EC2. We'll call EC2 APIs to fetch instance details.
ec2_client     = boto3.client('ec2', region_name="us-east-1")

# Create an HTTP client. We’ll use this to POST messages into Slack (via a webhook URL).
http           = urllib3.PoolManager()

# === FIXED SETTINGS: Update these once when you configure the Lambda ===

# Which AI model to use in Bedrock (this one is Claude v2 by Anthropic).
MODEL_ID          = "anthropic.claude-v2"

# The ARN (unique identifier) of the SNS topic where we publish remediation emails.
# Your CloudWatch alarm also publishes to this same topic.
SNS_TOPIC_ARN     = "arn:aws:sns:us-east-1:657506130129:SmartOpsAlertTopic"

# The Slack Webhook URL for your Slack Workflow trigger.
# Configure this as an environment variable in your Lambda settings.
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")


def lambda_handler(event, context):
    """
    Entry point for AWS Lambda.

    This function runs whenever a CloudWatch alarm fires (via SNS).
    Steps:
      1) Read and log the incoming SNS event.
      2) Unwrap the alarm details from the SNS message.
      3) Avoid processing messages that this Lambda itself sent earlier.
      4) Extract which EC2 instance triggered the alarm.
      5) Fetch detailed metadata about that EC2 instance.
      6) Build a prompt and ask Bedrock AI for remediation advice.
      7) Send an email (via SNS) containing the instance details + advice.
      8) Send the same structured data to Slack via your workflow’s trigger.
      9) Return a success response.
    """

    # 1) Log the entire incoming event (helps with debugging if something goes wrong).
    print("Received event:", json.dumps(event))

    # SNS wraps the actual CloudWatch alarm info in event['Records'][0]['Sns']
    record = event['Records'][0]['Sns']

    # 2) Check if this message carries a "source=remediation" attribute.
    #    We add that attribute when we send our own SNS email, so we don't re-process it.
    attrs = record.get('MessageAttributes', {})
    if attrs.get('source', {}).get('StringValue') == 'remediation':
        print("Skipping previously remediated notification.")
        return {'statusCode': 200, 'body': 'Skipped remediation message'}

    # 3) Try to parse the SNS message (it's a JSON string) into a Python dict.
    try:
        sns_msg = json.loads(record['Message'])
    except Exception as e:
        # If parsing fails, log the error and stop execution with a 400 error code.
        print("Error parsing SNS message:", str(e))
        print("Raw message was:", record['Message'])
        return {'statusCode': 400, 'body': 'Invalid SNS message format'}

    # 4) Extract the alarm name and the time it changed state.
    alarm_name = sns_msg.get('AlarmName', 'UnknownAlarm')
    timestamp  = sns_msg.get('StateChangeTime', 'UnknownTime')

    # 5) Look for the EC2 InstanceId in the alarm’s trigger dimensions.
    #    CloudWatch alarms include a "Trigger.Dimensions" array with keys like "InstanceId".
    instance_id = None
    trigger = sns_msg.get('Trigger', {}) or sns_msg.get('trigger', {})
    for dim in trigger.get('Dimensions', trigger.get('dimensions', [])):
        # Dimensions might use uppercase 'Name'/'Value' or lowercase
        name  = dim.get('Name') or dim.get('name')
        value = dim.get('Value') or dim.get('value')
        if name == 'InstanceId':
            instance_id = value
            break

    # 6) If we found an InstanceId, call EC2.describe_instances to get its metadata.
    if instance_id:
        try:
            resp = ec2_client.describe_instances(InstanceIds=[instance_id])
            inst = resp['Reservations'][0]['Instances'][0]

            # Extract various fields from the instance object
            name_tag       = next((t['Value'] for t in inst.get('Tags', []) if t['Key']=='Name'), 'N/A')
            instance_type  = inst.get('InstanceType', 'N/A')
            state          = inst.get('State', {}).get('Name', 'N/A')
            az             = inst.get('Placement', {}).get('AvailabilityZone', 'N/A')
            launch_time    = inst.get('LaunchTime').isoformat()
            private_ip     = inst.get('PrivateIpAddress', 'N/A')
            public_ip      = inst.get('PublicIpAddress', 'N/A')
            ami_id         = inst.get('ImageId', 'N/A')
            key_name       = inst.get('KeyName', 'N/A')
            subnet_id      = inst.get('SubnetId', 'N/A')
            vpc_id         = inst.get('VpcId', 'N/A')
            sg_list        = ", ".join(sg['GroupName'] for sg in inst.get('SecurityGroups', [])) or 'N/A'

            # Combine them into a multi-line string for easy inclusion in emails/slack
            resource_details = (
                f"Name: {name_tag}\n"
                f"InstanceId: {instance_id}\n"
                f"Type: {instance_type}\n"
                f"State: {state}\n"
                f"Availability Zone: {az}\n"
                f"Launched: {launch_time}\n"
                f"Private IP: {private_ip}\n"
                f"Public IP: {public_ip}\n"
                f"AMI ID: {ami_id}\n"
                f"Key Name: {key_name}\n"
                f"Subnet ID: {subnet_id}\n"
                f"VPC ID: {vpc_id}\n"
                f"Security Groups: {sg_list}"
            )
        except Exception as e:
            # If EC2 lookup fails, note it but continue
            print("Error fetching EC2 details:", str(e))
            resource_details = f"InstanceId: {instance_id} (failed to fetch full details)"
    else:
        # No instance ID found in the alarm
        resource_details = "No EC2 instance ID found in alarm."

    # 7) Build a detailed prompt for Bedrock AI
    prompt = (
        f"Human: A CloudWatch alarm named '{alarm_name}' for EC2 instance {instance_id or 'Unknown'} "
        f"fired at {timestamp} due to sustained CPU usage (>= 75%).\n\n"
        "Provide a detailed troubleshooting and remediation plan suitable for a cloud engineer. "
        "Include common causes, diagnostic steps (e.g., using CloudWatch metrics, top/htop), "
        "and remediation actions (e.g., resizing the instance, application optimization, Auto Scaling). "
        "Then list at least five relevant documentation links: two AWS docs, two best-practice articles, "
        "and one community tutorial. Each link on its own line.\n"
        "Assistant:"
    )
    bedrock_payload = json.dumps({
        "prompt": prompt,
        "max_tokens_to_sample": 500,  # Maximum length of AI response
        "temperature": 0.5            # Controls randomness (0 = repeatable, 1 = creative)
    })

    # 8) Call Bedrock to get the AI-generated advice
    try:
        response = bedrock_client.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=bedrock_payload
        )
        body   = json.loads(response["body"].read())
        advice = body.get("completion", "No advice returned.")
    except Exception as e:
        print("Error invoking Bedrock:", str(e))
        advice = "Could not retrieve remediation advice from Bedrock."

    # 9) Prepare the email content
    subject = f"[Alert] EC2 Alarm: {alarm_name}"
    message = (
        f"Alarm: {alarm_name}\n"
        f"Time: {timestamp}\n\n"
        f"=== Resource Details ===\n{resource_details}\n\n"
        f"=== Troubleshooting & Remediation ===\n{advice}"
    )

    # Send the email via SNS and tag it so we don't loop
    try:
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message,
            MessageAttributes={
                "source": {
                    "DataType":    "String",
                    "StringValue": "remediation"
                }
            }
        )
        print("Sent remediation email with resource details.")
    except Exception as e:
        print("Error sending SNS message:", str(e))

    # 10) Send the same structured data to Slack Workflow trigger
    if SLACK_WEBHOOK_URL:
        slack_payload = {
            "alarm_name":       alarm_name,
            "timestamp":        timestamp,
            "resource_details": resource_details,
            "advice":           advice
        }
        try:
            resp = http.request(
                "POST",
                SLACK_WEBHOOK_URL,
                body=json.dumps(slack_payload).encode('utf-8'),
                headers={"Content-Type": "application/json"}
            )
            print("Slack Workflow trigger status:", resp.status)
        except Exception as e:
            print("Error sending to Slack Workflow:", str(e))
    else:
        print("SLACK_WEBHOOK_URL not configured; skipping Slack notification.")

    # 11) Return a success response so AWS knows the Lambda executed successfully
    return {
        'statusCode': 200,
        'body': json.dumps({'status': 'ok'})
    }
```

   * **Deploy** the code.
6. **Ensure Trigger**:

   * Designer → Add trigger → CloudWatch Alarms → `POC-HighCPU`
   * **Add**

---

### Step 5: Test End-to-End

1. **Simulate CPU Spike** on EC2:

   ```bash
   sudo yum install -y stress    # Install stress
   stress --cpu 2 --timeout 180 # 2 vCPUs at 100% for 3 minutes
   ```
2. **Check Alarm**: CloudWatch → Alarms → `POC-HighCPU` → State = ALARM
3. **View Logs**: Lambda → Monitor → View logs in CloudWatch → latest stream → find the printed advice. Advice also emailed to specified IDs directly with AWS links!

---

By following these exact steps and using the commented code, even AWS newcomers can deploy this POC and see AI-generated remediation advice from Amazon Bedrock whenever their EC2 instance’s CPU spikes.
