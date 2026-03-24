from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils.crypto import get_random_string
import getpass


class Command(BaseCommand):
    help = 'Creates a new owner (staff) user for the turf application'

    def add_arguments(self, parser):
        parser.add_argument('--username', type=str, help='Username for the owner')
        parser.add_argument('--email', type=str, help='Email for the owner')
        parser.add_argument('--password', type=str, help='Password for the owner')
        parser.add_argument('--noinput', action='store_true', help='Create a random password if not provided')

    def handle(self, *args, **kwargs):
        username = kwargs.get('username')
        email = kwargs.get('email')
        password = kwargs.get('password')
        noinput = kwargs.get('noinput')

        # Interactive mode if arguments are not provided
        if not username:
            username = input('Enter username for owner account: ')
        
        if not email:
            email = input('Enter email for owner account: ')
        
        if not password and not noinput:
            password = getpass.getpass('Enter password for owner account: ')
            password_confirm = getpass.getpass('Confirm password: ')
            
            if password != password_confirm:
                self.stdout.write(self.style.ERROR('Passwords do not match.'))
                return
        
        # Generate random password if requested
        if not password and noinput:
            password = get_random_string(12)
        
        # Check if user already exists
        if User.objects.filter(username=username).exists():
            self.stdout.write(self.style.WARNING(f'User with username "{username}" already exists.'))
            update = input('Do you want to update this user to staff status? (y/n): ')
            if update.lower() == 'y':
                user = User.objects.get(username=username)
                user.is_staff = True
                user.save()
                self.stdout.write(self.style.SUCCESS(f'User "{username}" has been updated to staff status.'))
            return
        
        # Create the new user
        try:
            user = User.objects.create_user(username=username, email=email, password=password)
            user.is_staff = True
            user.save()
            
            self.stdout.write(self.style.SUCCESS(f'Owner user "{username}" created successfully!'))
            
            if noinput:
                self.stdout.write(self.style.WARNING(f'Generated password: {password}'))
                self.stdout.write(self.style.WARNING('Please save this password securely.'))
        
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error creating user: {str(e)}')) 