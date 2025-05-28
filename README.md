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
import json             # Used to work with JSON data (convert text to dictionary and vice versa)
import boto3            # AWS SDK for Python to interact with AWS services
import os               # Provides access to environment variables
import urllib3          # Used to send HTTP requests (e.g., to Slack)

# === SETUP CONNECTIONS TO AWS SERVICES AND SLACK ===

# Create a connection to Amazon SNS, which is used to send email or SMS alerts
sns_client = boto3.client('sns')

# Create a connection to Amazon Bedrock, a service that can generate AI-generated text (we use it for remediation advice)
bedrock_client = boto3.client('bedrock-runtime', region_name="us-east-1")

# Create a connection for making HTTP POST requests (used to notify Slack)
http = urllib3.PoolManager()

# === SET FIXED SETTINGS FOR THE SCRIPT ===

# The AI model we'll use (Claude v2 from Anthropic)
MODEL_ID = "anthropic.claude-v2"

# This is the ARN (Amazon Resource Name) of the SNS topic we'll send alerts to
SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:657506130129:SmartOpsAlertTopic"

# Slack Webhook URL is read from environment variables (kept secure and not hardcoded)
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

# === MAIN FUNCTION THAT RUNS WHEN THE LAMBDA IS TRIGGERED ===
def lambda_handler(event, context):
    # Print the entire incoming event for debugging purposes (helps when troubleshooting)
    print("Received event:", json.dumps(event))

    # From the incoming event, extract the actual SNS message part
    record = event['Records'][0]['Sns']

    # Prevent the function from reacting to messages it created itself (avoids endless loops)
    attrs = record.get('MessageAttributes', {})
    if attrs.get('source', {}).get('StringValue') == 'remediation':
        print("Skipping remediated notification to avoid loop.")
        return {'statusCode': 200, 'body': 'Skipped remediation message'}

    # Try to convert the SNS message from text to a Python dictionary
    try:
        sns_msg = json.loads(record['Message'])
    except Exception as e:
        # If it fails, print the error and original message, then stop execution
        print("Error parsing SNS message:", str(e))
        print("Raw message was:", record['Message'])
        return {'statusCode': 400, 'body': 'Invalid SNS message format'}

    # Extract useful details: alarm name and the time it triggered
    alarm_name = sns_msg.get('AlarmName', 'UnknownAlarm')
    timestamp = sns_msg.get('StateChangeTime', 'UnknownTime')

    # === CONSTRUCT THE QUESTION TO ASK THE AI ===
    # We're pretending to be a user asking the AI for help resolving a high-CPU EC2 alarm
    prompt = (
        f"Human: EC2 Alarm '{alarm_name}' fired at {timestamp} due to CPU ≥ 75%.\n"
        "Provide a concise paragraph describing how to troubleshoot and resolve this issue. "
        "Then list at least two official AWS public documentation URLs (one per line) that "
        "would help a cloud engineer implement the fix.\n"
        "Assistant:"
    )

    # Package the prompt and additional options into a format Bedrock expects
    bedrock_payload = json.dumps({
        "prompt": prompt,
        "max_tokens_to_sample": 400,  # Limit how much text the AI can return
        "temperature": 0.5            # Controls creativity (0 = deterministic, 1 = very creative)
    })

    # === CALL THE BEDROCK AI TO GET ADVICE ===
    try:
        # Ask the AI model for help
        response = bedrock_client.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=bedrock_payload
        )
        # Read and parse the response
        body = json.loads(response["body"].read())
        advice = body.get("completion", "No advice returned.")  # Extract the AI's response
    except Exception as e:
        # If something goes wrong, report it and use a fallback message
        print("Error invoking Bedrock:", str(e))
        advice = "Could not retrieve remediation advice from Bedrock."

    # Print the AI's remediation advice to the logs for visibility
    print("Remediation advice + links:\n", advice)

    # === SEND THE ADVICE VIA EMAIL USING SNS ===

    # Create the email subject and body
    subject = f"[Alert] EC2 Alarm: {alarm_name}"
    message = (
        f"The EC2 CloudWatch alarm '{alarm_name}' was triggered at {timestamp}.\n\n"
        "=== Troubleshooting & Remediation ===\n"
        f"{advice}"
    )

    try:
        # Send the email using SNS
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message,
            MessageAttributes={
                "source": {
                    "DataType": "String",
                    "StringValue": "remediation"  # Mark this message as coming from remediation logic
                }
            }
        )
        print("Remediation advice sent via SNS.")
    except Exception as e:
        # Log any issues when trying to send the email
        print("Error sending SNS message:", str(e))

    # === SEND THE SAME INFO TO SLACK (IF SET UP) ===
    if SLACK_WEBHOOK_URL:
        # Create the JSON message to send to Slack
        slack_payload = {
            "alarm_name": alarm_name,
            "timestamp": timestamp,
            "advice": advice
        }
        try:
            # Use HTTP POST to send the message to the Slack Workflow
            resp = http.request(
                "POST",
                SLACK_WEBHOOK_URL,
                body=json.dumps(slack_payload).encode('utf-8'),
                headers={"Content-Type": "application/json"}
            )
            print("Slack notification status:", resp.status)
        except Exception as e:
            # Log any error when trying to contact Slack
            print("Error sending Slack message:", str(e))
    else:
        # Slack is optional – if it's not set up, just skip it
        print("SLACK_WEBHOOK_URL not configured; skipping Slack notification.")

    # === FINALLY, RETURN A SUCCESS RESPONSE BACK TO AWS ===
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
