import json
import os
import boto3
import uuid
from datetime import datetime
import urllib.parse

# ==========================================
# AWS CLIENT INITIALIZATION
# ==========================================
# Initialize all required AWS service clients
s3 = boto3.client('s3')           # For accessing S3 bucket and objects
textract = boto3.client('textract')  # For extracting text from receipt images
dynamodb = boto3.resource('dynamodb')  # For database operations
ses = boto3.client('ses')         # For sending email notifications

# ==========================================
# ENVIRONMENT VARIABLES CONFIGURATION
# ==========================================
# These environment variables can be set in Lambda console
# They allow us to change configuration without modifying code
DYNAMODB_TABLE = os.environ.get('DYNAMODB_TABLE', 'Table_name')  # DynamoDB table name
SES_SENDER_EMAIL = os.environ.get('SES_SENDER_EMAIL', 'sender_email')  # Verified sender email
SES_RECIPIENT_EMAIL = os.environ.get('SES_RECIPIENT_EMAIL', 'receiver_email')  # Email to receive notifications

def lambda_handler(event, context):
    """
    Main Lambda function handler - This is the entry point when Lambda is triggered
    
    Args:
        event: Contains information about the S3 event that triggered this function
        context: Lambda runtime information (not used in this function)
    
    Returns:
        Dictionary with statusCode and response body
    """
    try:
        # ==========================================
        # EXTRACT S3 EVENT INFORMATION
        # ==========================================
        # When a file is uploaded to S3, it triggers this Lambda function
        # The event contains details about which bucket and file triggered it
        bucket = event['Records'][0]['s3']['bucket']['name']  # S3 bucket name
        
        # URL decode the key to handle spaces and special characters in filenames
        # Example: "my%20receipt.jpg" becomes "my receipt.jpg"
        key = urllib.parse.unquote_plus(event['Records'][0]['s3']['object']['key'])

        print(f"Processing receipt from {bucket}/{key}")

        # ==========================================
        # VERIFY S3 OBJECT EXISTS
        # ==========================================
        # Before processing, make sure the file actually exists and is accessible
        try:
            s3.head_object(Bucket=bucket, Key=key)  # Check if object exists
            print(f"Object verification successful: {bucket}/{key}")
        except Exception as e:
            print(f"Object verification failed: {str(e)}")
            raise Exception(f"Unable to access object {key} in bucket {bucket}: {str(e)}")

        # ==========================================
        # MAIN PROCESSING PIPELINE
        # ==========================================
        # Step 1: Use AWS Textract to extract text and data from the receipt image
        receipt_data = process_receipt_with_textract(bucket, key)

        # Step 2: Save the extracted data to DynamoDB for permanent storage
        store_receipt_in_dynamodb(receipt_data, bucket, key)

        # Step 3: Send an email notification with the processed receipt details
        send_email_notification(receipt_data)

        # Return success response
        return {
            'statusCode': 200,
            'body': json.dumps('Receipt processed successfully!')
        }
        
    except Exception as e:
        # Log any errors that occur during processing
        print(f"Error processing receipt: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps(f'Error: {str(e)}')
        }

def process_receipt_with_textract(bucket, key):
    """
    Process receipt image using AWS Textract's AnalyzeExpense operation
    This is specifically designed for receipts and invoices
    
    Args:
        bucket: S3 bucket name containing the receipt
        key: S3 object key (filename) of the receipt
    
    Returns:
        Dictionary containing extracted receipt data
    """
    try:
        print(f"Calling Textract analyze_expense for {bucket}/{key}")
        
        # ==========================================
        # CALL AWS TEXTRACT SERVICE
        # ==========================================
        # analyze_expense is specifically designed for receipts and invoices
        # It can automatically identify fields like total, date, vendor, items
        response = textract.analyze_expense(
            Document={
                'S3Object': {
                    'Bucket': bucket,
                    'Name': key
                }
            }
        )
        print("Textract analyze_expense call successful")
        
    except Exception as e:
        print(f"Textract analyze_expense call failed: {str(e)}")
        raise

    # ==========================================
    # INITIALIZE RECEIPT DATA STRUCTURE
    # ==========================================
    # Generate a unique ID for this receipt using UUID
    receipt_id = str(uuid.uuid4())

    # Create a standardized data structure for the receipt
    # Set default values in case Textract can't extract certain fields
    receipt_data = {
        'receipt_id': receipt_id,
        'date': datetime.now().strftime('%Y-%m-%d'),  # Default to today's date
        'vendor': 'Unknown',  # Default vendor name
        'total': '0.00',      # Default total amount
        'items': [],          # List to store individual items
        's3_path': f"s3://{bucket}/{key}"  # Reference to original file location
    }

    # ==========================================
    # EXTRACT DATA FROM TEXTRACT RESPONSE
    # ==========================================
    # Textract response contains ExpenseDocuments array
    if 'ExpenseDocuments' in response and response['ExpenseDocuments']:
        expense_doc = response['ExpenseDocuments'][0]  # Get first (and usually only) document

        # ==========================================
        # PROCESS SUMMARY FIELDS
        # ==========================================
        # Summary fields contain high-level information like total, date, vendor
        if 'SummaryFields' in expense_doc:
            for field in expense_doc['SummaryFields']:
                # Extract field type and value from Textract response
                field_type = field.get('Type', {}).get('Text', '')
                value = field.get('ValueDetection', {}).get('Text', '')

                # Map Textract field types to our data structure
                if field_type == 'TOTAL':
                    receipt_data['total'] = value
                elif field_type == 'INVOICE_RECEIPT_DATE':
                    # Try to use the extracted date, fall back to default if parsing fails
                    try:
                        receipt_data['date'] = value
                    except:
                        # Keep the default date if parsing fails
                        pass
                elif field_type == 'VENDOR_NAME':
                    receipt_data['vendor'] = value

        # ==========================================
        # PROCESS LINE ITEMS
        # ==========================================
        # Line items contain individual products/services on the receipt
        if 'LineItemGroups' in expense_doc:
            for group in expense_doc['LineItemGroups']:
                if 'LineItems' in group:
                    for line_item in group['LineItems']:
                        item = {}  # Dictionary to store individual item data
                        
                        # Extract fields for each line item
                        for field in line_item.get('LineItemExpenseFields', []):
                            field_type = field.get('Type', {}).get('Text', '')
                            value = field.get('ValueDetection', {}).get('Text', '')

                            # Map field types to item properties
                            if field_type == 'ITEM':
                                item['name'] = value
                            elif field_type == 'PRICE':
                                item['price'] = value
                            elif field_type == 'QUANTITY':
                                item['quantity'] = value

                        # Only add items that have at least a name
                        if 'name' in item:
                            receipt_data['items'].append(item)

    # Log the extracted data for debugging
    print(f"Extracted receipt data: {json.dumps(receipt_data)}")
    return receipt_data

def store_receipt_in_dynamodb(receipt_data, bucket, key):
    """
    Store the extracted receipt data in DynamoDB table
    
    Args:
        receipt_data: Dictionary containing extracted receipt information
        bucket: S3 bucket name (for reference)
        key: S3 object key (for reference)
    """
    try:
        # ==========================================
        # GET DYNAMODB TABLE REFERENCE
        # ==========================================
        table = dynamodb.Table(DYNAMODB_TABLE)

        # ==========================================
        # PREPARE ITEMS DATA FOR DYNAMODB
        # ==========================================
        # Convert items list to a format that DynamoDB can store
        # Ensure all items have required fields with default values
        items_for_db = []
        for item in receipt_data['items']:
            items_for_db.append({
                'name': item.get('name', 'Unknown Item'),
                'price': item.get('price', '0.00'),
                'quantity': item.get('quantity', '1')
            })

        # ==========================================
        # CREATE DYNAMODB ITEM
        # ==========================================
        # Structure the data according to our DynamoDB table schema
        # receipt_id = partition key, date = sort key
        db_item = {
            'receipt_id': receipt_data['receipt_id'],    # Partition key
            'date': receipt_data['date'],                # Sort key
            'vendor': receipt_data['vendor'],            # Vendor name
            'total': receipt_data['total'],              # Total amount
            'items': items_for_db,                       # List of items
            's3_path': receipt_data['s3_path'],          # Reference to original file
            'processed_timestamp': datetime.now().isoformat()  # When this was processed
        }

        # ==========================================
        # INSERT INTO DYNAMODB
        # ==========================================
        table.put_item(Item=db_item)
        print(f"Receipt data stored in DynamoDB: {receipt_data['receipt_id']}")
        
    except Exception as e:
        print(f"Error storing data in DynamoDB: {str(e)}")
        raise

def send_email_notification(receipt_data):
    """
    Send an email notification with receipt processing results
    
    Args:
        receipt_data: Dictionary containing extracted receipt information
    """
    try:
        # ==========================================
        # FORMAT ITEMS FOR EMAIL DISPLAY
        # ==========================================
        # Create HTML list of items for the email
        items_html = ""
        for item in receipt_data['items']:
            name = item.get('name', 'Unknown Item')
            price = item.get('price', 'N/A')
            quantity = item.get('quantity', '1')
            items_html += f"<li>{name} - ${price} x {quantity}</li>"

        # Handle case where no items were detected
        if not items_html:
            items_html = "<li>No items detected</li>"

        # ==========================================
        # CREATE HTML EMAIL BODY
        # ==========================================
        # Create a nicely formatted HTML email with all receipt details
        html_body = f"""
        <html>
        <body>
            <h2>📧 Receipt Processing Notification</h2>
            <p><strong>Receipt ID:</strong> {receipt_data['receipt_id']}</p>
            <p><strong>Vendor:</strong> {receipt_data['vendor']}</p>
            <p><strong>Date:</strong> {receipt_data['date']}</p>
            <p><strong>Total Amount:</strong> ${receipt_data['total']}</p>
            <p><strong>S3 Location:</strong> {receipt_data['s3_path']}</p>

            <h3>📋 Items:</h3>
            <ul>
                {items_html}
            </ul>

            <p>✅ The receipt has been processed and stored in DynamoDB.</p>
        </body>
        </html>
        """

        # ==========================================
        # SEND EMAIL USING AWS SES
        # ==========================================
        ses.send_email(
            Source=SES_SENDER_EMAIL,  # Must be a verified email address in SES
            Destination={
                'ToAddresses': [SES_RECIPIENT_EMAIL]  # Can be a list of multiple recipients
            },
            Message={
                'Subject': {
                    'Data': f"Receipt Processed: {receipt_data['vendor']} - ${receipt_data['total']}"
                },
                'Body': {
                    'Html': {
                        'Data': html_body
                    }
                }
            }
        )

        print(f"Email notification sent to {SES_RECIPIENT_EMAIL}")
        
    except Exception as e:
        print(f"Error sending email notification: {str(e)}")
        # Continue execution even if email fails - don't let email errors stop the process
        print("Continuing execution despite email error")