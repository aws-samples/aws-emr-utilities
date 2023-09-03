import sys
import os
import datetime
import shutil
import xml.etree.ElementTree as ET
import subprocess

def generate_property_element(name, value, description):
    property_elem = ET.Element('property')
    name_elem = ET.SubElement(property_elem, 'name')
    name_elem.text = name
    value_elem = ET.SubElement(property_elem, 'value')
    value_elem.text = value
    description_elem = ET.SubElement(property_elem, 'description')
    description_elem.text = description
    return property_elem

def generate_xml_from_properties(properties):
    root = ET.Element('configuration')

    for key, value in properties.items():
        property_elem = generate_property_element(key, value, '')
        root.append(property_elem)

    xml_str = ET.tostring(root, encoding='utf-8').decode('utf-8')
    formatted_xml = xml_str.replace('><', '>\n    <')

    xml_content = f'<?xml version="1.0"?>\n{formatted_xml}'

    return xml_content

def backup_existing_file(file_path, backup_dir):
    timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    backup_file = os.path.join(backup_dir, f'capacity-scheduler-{timestamp}.xml')
    shutil.copy(file_path, backup_file)

def replace_file(file_path, new_content):
    with open(file_path, 'w') as file:
        file.write(new_content)

def refresh_yarn_admin():
    subprocess.run(['yarn', 'rmadmin', '-refreshQueues'], check=True)

def extract_properties(element, prefix='', properties=None):
    if properties is None:
        properties = {}
    
    for child in element:
        if child.tag == 'property':
            name_elem = child.find('name')
            value_elem = child.find('value')
            if name_elem is not None and value_elem is not None:
                name = name_elem.text.strip()
                value = value_elem.text.strip() if value_elem.text is not None else ''
                properties[prefix + name] = value
        else:
            extract_properties(child, prefix, properties)
    
    return properties

def write_properties_to_file(properties, file_path):
    with open(file_path, 'w') as f:
        for key, value in properties.items():
            f.write(key + '=' + value + '\n')

def modify_queues_script(properties_file_path):
    properties = {}
    existing_queues = []
    with open(properties_file_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                key, value = line.split('=', 1)
                properties[key] = value
                if key == 'yarn.scheduler.capacity.root.queues':
                    existing_queues = value.split(',')

    action = input("Do you want to add or remove a queue? (add/remove): ")

    if action == 'add':
        new_queue = input("Enter the name of the new queue: ")
        existing_queues.append(new_queue)
        new_properties = {}

        valid_property_names = [
            'capacity', 'maximum-capacity', 'user-limit-factor', 'accessible-node-labels',
            'maximum-allocation-mb', 'maximum-allocation-vcores', 'maximum-applications',
            'maximum-am-resource-percent', 'max-parallel-apps', 'state', 'acl_submit_applications',
            'acl_administer_queue'
        ]

        print("Valid property names: " + ', '.join(valid_property_names))

        while True:
            prop_name = input("Enter a property name (or press Enter to finish): ")
            if not prop_name:
                break
            if prop_name not in valid_property_names:
                print("Invalid property name. Please enter one of the following: "
                      + ', '.join(valid_property_names))
                continue
            prop_value = input("Enter the value for " + prop_name + ": ")
            new_properties[prop_name] = prop_value

        properties['yarn.scheduler.capacity.root.queues'] = ','.join(existing_queues)
        for prop_name, prop_value in new_properties.items():
            properties['yarn.scheduler.capacity.root.' + new_queue + '.' + prop_name] = prop_value

        print("Queue " + new_queue + " and its properties have been added successfully.")

    elif action == 'remove':
        queue_to_remove = input("Enter the name of the queue to remove: ")
        if queue_to_remove in existing_queues:
            existing_queues.remove(queue_to_remove)
            queue_properties = [prop for prop in properties if prop.startswith('yarn.scheduler.capacity.root.' + queue_to_remove + '.')]
            for prop in queue_properties:
                del properties[prop]
            properties['yarn.scheduler.capacity.root.queues'] = ','.join(existing_queues)
            print("Queue " + queue_to_remove + " and its properties have been removed successfully.")
        else:
            print("Queue " + queue_to_remove + " does not exist. No changes were made.")

    with open(properties_file_path, 'w') as f:
        for key, value in properties.items():
            f.write(key + '=' + value + '\n')

def generate_xml_script():
    properties_file_path = '/home/hadoop/capacity-scheduler.properties'
    properties = {}

    with open(properties_file_path, 'r') as prop_file:
        for line in prop_file:
            line = line.strip()
            if line.startswith('#') or line == '':
                continue
            if '=' in line:
                name, value = line.split('=', 1)
                properties[name] = value

    xml_content = generate_xml_from_properties(properties)

    target_file = '/etc/hadoop/conf/capacity-scheduler.xml'
    backup_dir = '/etc/hadoop/conf/capacity-scheduler-backups'

    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir, exist_ok=True)
    backup_existing_file(target_file, backup_dir)

    replace_file(target_file, xml_content)
    refresh_yarn_admin()

    print(f'Replaced {target_file} with the generated XML.')
    print(f'Backup file created at {backup_dir}.')
    print('YARN admin refreshed.')

def create_properties_from_xml_script(xml_file_path, properties_file_path):
    try:
        extracted_properties = extract_properties(ET.parse(xml_file_path).getroot())
        write_properties_to_file(extracted_properties, properties_file_path)
        print("Properties successfully extracted and saved to {}.".format(properties_file_path))
    except ET.ParseError:
        print("Error: Failed to parse capacity-scheduler.xml. Check if the XML file is valid.")
        exit(1)

def main():
    while True:
        print("1) Modify queues")
        print("2) Generate XML representation of properties")
        print("3) Create the 'capacity-scheduler.properties' file based on XML")
        print("4) Exit")

        choice = input("Enter your choice: ")

        if choice == '1':
            modify_queues_script('/home/hadoop/capacity-scheduler.properties')

        elif choice == '2':
            generate_xml_script()

        elif choice == '3':
            xml_file_path = '/etc/hadoop/conf/capacity-scheduler.xml'
            properties_file_path = '/home/hadoop/capacity-scheduler.properties'
            create_properties_from_xml_script(xml_file_path, properties_file_path)

        elif choice == '4':
            print("Exiting the script.")
            sys.exit()

        else:
            print("Invalid choice. Please enter a valid option.")

if __name__ == "__main__":
    main()

