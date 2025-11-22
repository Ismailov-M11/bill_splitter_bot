import logging
from decimal import Decimal

# Mocking necessary parts for the test if we were to copy the function, 
# but let's try to import from bot.py first. 
# We need to make sure bot.py is in the path.
import sys
import os

# Add current directory to sys.path
sys.path.append(os.getcwd())

try:
    from bot import _format_webapp_message
except ImportError:
    print("Could not import bot.py. Make sure you are running this from the correct directory.")
    sys.exit(1)

# Mock data that represents what the Web App SHOULD send (with Group IDs)
# Scenario:
# User 1, User 2, User 3
# Dish 1 (100k) -> Group 1 (User 1, User 2)
# Dish 2 (140k) -> Group 2 (User 2, User 3)
# Service 12%

mock_data = {
    'type': 'calculation',
    'servicePercent': 12,
    'participants': [
        {'id': 'p_1', 'name': 'User 1'},
        {'id': 'p_2', 'name': 'User 2'},
        {'id': 'p_3', 'name': 'User 3'}
    ],
    'groups': [
        {'id': 'g_1', 'name': 'Group 1', 'memberIds': ['p_1', 'p_2']},
        {'id': 'g_2', 'name': 'Group 2', 'memberIds': ['p_2', 'p_3']}
    ],
    'dishes': [
        {
            'id': 'd_1', 
            'name': 'Dish 1', 
            'qty': 1, 
            'totalPrice': 100000, 
            'flatAssignments': ['g_1'] # Corrected: sending Group ID
        },
        {
            'id': 'd_2', 
            'name': 'Dish 2', 
            'qty': 1, 
            'totalPrice': 140000, 
            'flatAssignments': ['g_2'] # Corrected: sending Group ID
        }
    ]
}

print("--- Running Test with Mock Data ---")
result = _format_webapp_message(mock_data)
print("\n--- Result ---")
print(result)

# Expected:
# Dish 1 (100k) -> User 1 (50k), User 2 (50k)
# Dish 2 (140k) -> User 2 (70k), User 3 (70k)
# Totals Base:
# User 1: 50k
# User 2: 120k
# User 3: 70k
# Service 12%:
# User 1: 6k -> Total 56k
# User 2: 14.4k -> Total 134.4k
# User 3: 8.4k -> Total 78.4k
