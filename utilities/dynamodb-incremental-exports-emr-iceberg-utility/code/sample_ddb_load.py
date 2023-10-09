import boto3
# Initialize DynamoDB resource
dynamodb = boto3.resource('dynamodb')
# Insert sample items
def insert_items():
    table = dynamodb.Table('SampleTable')
    items = [
        {
            'PrimaryKey': 'Item1',
            'StringAttribute': 'SampleString1',
            'NumberAttribute': 123,
            'BinaryAttribute': b'BinaryData1',
            'BooleanAttribute': True,
            'NullAttribute': None,
            'ListAttribute': ['value1', 1, {'NestedKey1': 'NestedValue1'}],
            'MapAttribute': {
                'key1': 'value1',
                'NestedMap': {
                    'NestedKey1': 'NestedValue1'
                }
            }
        },
        {
            'PrimaryKey': 'Item2',
            'StringAttribute': 'SampleString2',
            'NumberAttribute': 456,
            'BinaryAttribute': b'BinaryData2',
            'BooleanAttribute': False,
            'NullAttribute': None,
            'ListAttribute': ['value2', 2, [1, 2, 3]],
            'MapAttribute': {
                'key2': 'value2',
                'KeyWithListValue': ['ListValue1', 'ListValue2']
            }
        },
        {
            'PrimaryKey': 'Item3',
            'StringAttribute': 'SampleString3',
            'NumberAttribute': 789,
            'BinaryAttribute': b'BinaryData3',
            'BooleanAttribute': True,
            'NullAttribute': None,
            'ListAttribute': ['value3', 3, {'NestedKey2': ['valueA', 'valueB']}],
            'MapAttribute': {
                'key3': 'value3',
                'NestedMap': {
                    'NestedKey2': {
                        'FurtherNestedKey': 'FurtherNestedValue'
                    }
                }
            }
        },
        {
            'PrimaryKey': 'Item4',
            'StringAttribute': 'SampleString4',
            'NumberAttribute': 101112,
            'BinaryAttribute': b'BinaryData4',
            'BooleanAttribute': False,
            'NullAttribute': None,
            'ListAttribute': ['value4', 4, [4, 5, 6]],
            'MapAttribute': {
                'key4': 'value4',
                'KeyWithListValue': ['ListValue3', 'ListValue4']
            }
        },
        {
            'PrimaryKey': 'Item5',
            'StringAttribute': 'SampleString5',
            'NumberAttribute': 131415,
            'BinaryAttribute': b'BinaryData5',
            'BooleanAttribute': True,
            'NullAttribute': None,
            'ListAttribute': ['value5', 5, {'NestedKey3': [7, 8, 9]}],
            'MapAttribute': {
                'key5': 'value5',
                'NestedMap': {
                    'NestedKey3': 'NestedValue3'
                }
            }
        }
    ]
    for item in items:
        table.put_item(Item=item)
if __name__ == "__main__":
    insert_items()
