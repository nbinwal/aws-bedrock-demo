# aws-bedrock-demo
---

## 🧠 How Does Amazon Bedrock Provide Remediation?

Foundation models (like Anthropic Claude) are trained on massive technical corpora (docs, blogs, forums). When you send a prompt, they generate responses based on patterns they've learned.

This POC:

- Monitors an EC2 instance's CPU utilization using CloudWatch.
- Triggers a Lambda function when CPU usage exceeds 75%.
- Invokes Amazon Bedrock to get remediation steps.

---

## 🛠️ Step-by-Step Implementation

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
   Sure — here is your exact code, unmodified:

```python
import json
import boto3

def lambda_handler(event, context):
    print("Received event:", json.dumps(event))

    # Extract alarm details
    detail = event.get('detail', {})
    alarm_name = detail.get('alarmName', 'UnknownAlarm')
    timestamp = detail.get('state', {}).get('timestamp', '')

    # Create the prompt
    user_prompt = (
        f"EC2 Alarm '{alarm_name}' fired at {timestamp} due to CPU ≥ 75%. "
        "Provide a numbered list of steps to troubleshoot and fix this issue."
    )

    # Prepare request body for Claude 3
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "messages": [
            {
                "role": "user",
                "content": user_prompt
            }
        ],
        "max_tokens": 500,
        "temperature": 0.5
    })

    # Bedrock client
    client = boto3.client("bedrock-runtime", region_name="us-east-1")

    # Your Inference Profile ARN for Claude 3.5
    model_id = "arn:aws:bedrock:us-east-1:657506130129:inference-profile/us.anthropic.claude-3-5-sonnet-20241022-v2:0"

    # Invoke the model
    response = client.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=body
    )

    # Parse response
    response_body = json.loads(response["body"].read())
    advice = ""

    if "content" in response_body:
        advice = "\n".join(part["text"] for part in response_body["content"] if "text" in part)
    else:
        advice = "No advice returned."

    print("Remediation advice:\n", advice)

    return {
        'statusCode': 200,
        'body': json.dumps({'advice': advice})
    }
```

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
3. **View Logs**: Lambda → Monitor → View logs in CloudWatch → latest stream → find the printed advice

---

## Troubleshooting Tips

* **Lambda Timeout**: Increase under Configuration → General configuration → Edit
* **IAM Errors**: Confirm `LambdaBedrockExecutionRole` has CloudWatch Logs + Bedrock policies
* **Region**: Verify Bedrock support in your region (us-east-1)
* **Agent Metrics**: Check CloudWatch Metrics to see incoming CPU data

---

By following these exact steps and using the commented code, even AWS newcomers can deploy this POC and see AI-generated remediation advice from Amazon Bedrock whenever their EC2 instance’s CPU spikes.
