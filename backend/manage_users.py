"""Manage accounts of the web service from the server (users.json, outside the repository).

  python manage_users.py add admin --name "Quản trị" --role admin     # the first admin; then use the admin page
  python manage_users.py add bs.an --name "BS. Nguyễn Văn An"          # role doctor by default; lab: --role lab
  python manage_users.py passwd bs.an
  python manage_users.py role bs.an --role lab
  python manage_users.py disable bs.an | enable bs.an | remove bs.an
  python manage_users.py list

Passwords are asked interactively and stored as scrypt hashes. The running service rereads the file:
no restart is needed, and a disabled or removed account is logged out at its next request.
While the file lists no account (or cannot be read), the service refuses every API request (503) unless
WEB_REQUIRE_ACCOUNTS=0 (local use), which falls back to WEB_ACCESS_TOKEN, then to no login.
"""
import argparse
import getpass
import sys
import time
from auth import ROLES, USERNAME, UserStore, hash_password, password_problem


def ask_password(username):
    while True:
        first = getpass.getpass('Mật khẩu mới: ')
        problem = password_problem(first, username)
        if problem:
            print(problem)
        elif first != getpass.getpass('Nhập lại: '):
            print('Hai lần nhập không khớp.')
        else:
            return first


def main(argv=None):
    parser = argparse.ArgumentParser(description='Quản lý tài khoản web Fe2Gen.')
    parser.add_argument('action', choices=['add', 'passwd', 'role', 'disable', 'enable', 'remove', 'list'])
    parser.add_argument('username', nargs='?')
    parser.add_argument('--name', help='Tên hiển thị, ví dụ "BS. Nguyễn Văn An"')
    parser.add_argument('--role', choices=sorted(ROLES), help='admin (quản trị), doctor (bác sĩ, mặc định), lab (phòng Lab)')
    parser.add_argument('--file', help='Đường dẫn users.json (mặc định WEB_USERS_FILE hoặc ~/prenatal_pubcasefinder/.deployment/users.json)')
    args = parser.parse_args(argv)
    store = UserStore(args.file)
    users = store.all()
    if args.action == 'list':
        print(f'{store.path}: {len(users)} tài khoản')
        for name, u in sorted(users.items()):
            state = 'ĐÃ KHÓA' if u.get('disabled') else 'chờ đổi mật khẩu' if u.get('must_change') else 'hoạt động'
            print(f"  {name:<20} {u.get('name', ''):<28} {ROLES.get(u.get('role', 'doctor'), '?'):<10} {state}")
        return 0
    username = (args.username or '').strip().lower()
    if not USERNAME.match(username):
        parser.error('Tên đăng nhập gồm 3–32 ký tự a-z, 0-9, dấu chấm, gạch ngang hoặc gạch dưới.')
    if args.action == 'add':
        if username in users:
            parser.error('Tài khoản đã có; dùng passwd để đổi mật khẩu.')
        users[username] = {'name': args.name or username, 'role': args.role or 'doctor',
                           'password': hash_password(ask_password(username)), 'disabled': False,
                           'created': time.strftime('%Y-%m-%d %H:%M')}
    elif username not in users:
        parser.error('Không có tài khoản ' + username)
    elif args.action == 'passwd':
        users[username].update(password=hash_password(ask_password(username)), must_change=False)
    elif args.action == 'role':
        if not args.role:
            parser.error('Cần --role admin|doctor|lab')
        users[username]['role'] = args.role
    elif args.action in ('disable', 'enable'):
        users[username]['disabled'] = args.action == 'disable'
    elif args.action == 'remove':
        del users[username]
    if args.name and args.action != 'add' and username in users:
        users[username]['name'] = args.name
    store.save(users)
    print(f'Đã cập nhật {username} trong {store.path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
