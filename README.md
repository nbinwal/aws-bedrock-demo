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
5. **Add/Replace Code given in lamdacode.py**

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
