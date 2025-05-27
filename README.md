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
import json
import boto3

# === Set up AWS service clients once (so we don’t redo it every time) ===
# SNS client: used to send emails (notifications) via an SNS topic
sns_client = boto3.client('sns')
# Bedrock client: used to call the AI model (Anthropic Claude v2) for troubleshooting advice
bedrock_client = boto3.client('bedrock-runtime', region_name="us-east-1")

# === Configuration constants ===
MODEL_ID = "anthropic.claude-v2"  # Which AI model to use
# The SNS topic where CloudWatch alarms arrive and where we publish remediation emails
SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:657506130129:SmartOpsAlertTopic"


def lambda_handler(event, context):
    """
    This Lambda function runs automatically whenever a CloudWatch alarm fires.
    1. It reads the alarm details from the incoming event.
    2. It asks Bedrock (the AI service) for troubleshooting steps plus AWS doc links.
    3. It emails that advice back out via the same SNS topic.
    """

    # 1) Show the full event in the logs for debugging if something goes wrong
    print("Received event:", json.dumps(event))

    # 2) Pull out the actual alarm data from the SNS wrapper
    try:
        # event['Records'][0]['Sns']['Message'] is a JSON string of the alarm details
        sns_msg = json.loads(event['Records'][0]['Sns']['Message'])
    except Exception as e:
        # If we can’t parse the alarm data, stop here and log the error
        print("Error parsing SNS message:", str(e))
        return {
            'statusCode': 400,
            'body': 'Invalid SNS message format'
        }

    # 3) Extract the alarm name and the time it changed state
    alarm_name = sns_msg.get('AlarmName', 'UnknownAlarm')
    timestamp = sns_msg.get('StateChangeTime', 'UnknownTime')

    # 4) Build the text prompt we send to the AI model.
    #    - It starts with "Human:" so Claude knows it’s our request.
    #    - It ends with "Assistant:" so Claude knows where to put its answer.
    #    - We explicitly ask for at least two official AWS docs URLs at the end.
    prompt = (
        f"Human: EC2 Alarm '{alarm_name}' fired at {timestamp} due to CPU ≥ 75%.\n"
        "Provide a concise paragraph describing how to troubleshoot and resolve this issue. "
        "Then list at least two official AWS public documentation URLs (one per line) that "
        "would help a cloud engineer implement the fix.\n"
        "Assistant:"
    )

    # Pack our prompt into the format Bedrock expects
    bedrock_payload = json.dumps({
        "prompt": prompt,
        "max_tokens_to_sample": 400,  # allow room for text + links
        "temperature": 0.5            # 0.5 = balanced creativity
    })

    # 5) Call the Bedrock AI service
    try:
        response = bedrock_client.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=bedrock_payload
        )
        # Read and parse the AI’s reply
        body = json.loads(response["body"].read())
        advice = body.get("completion", "No advice returned.")
    except Exception as e:
        # If the AI call fails, log and use a fallback message
        print("Error invoking Bedrock:", str(e))
        advice = "Could not retrieve remediation advice from Bedrock."

    # Log what we got back from the AI
    print("Remediation advice + links:\n", advice)

    # 6) Prepare the email subject and body
    subject = f"[Alert] EC2 Alarm: {alarm_name}"
    message = (
        f"The EC2 CloudWatch alarm '{alarm_name}' was triggered at {timestamp}.\n\n"
        "=== Troubleshooting & Remediation ===\n"
        f"{advice}"
    )

    # 7) Send the remediation email back out via SNS
    #    We add a message attribute "source=remediation" so our Lambda won’t
    #    accidentally re-trigger itself on this email.
    try:
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message,
            MessageAttributes={
                "source": {
                    "DataType": "String",
                    "StringValue": "remediation"
                }
            }
        )
        print("Remediation advice + links sent via SNS.")
    except Exception as e:
        # If sending fails, log the error
        print("Error sending SNS message:", str(e))

    # 8) Return success so AWS knows our Lambda ran fine
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
