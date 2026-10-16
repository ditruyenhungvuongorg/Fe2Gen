"""Manage doctor accounts of the web service (users.json, outside the repository).

  python manage_users.py add bs.an --name "BS. Nguyễn Văn An"
  python manage_users.py passwd bs.an
  python manage_users.py disable bs.an | enable bs.an | remove bs.an
  python manage_users.py list

Passwords are asked interactively and stored as scrypt hashes. The running service rereads the file:
no restart is needed, and a disabled or removed account is logged out at its next request.
While the file lists no account, the service keeps using WEB_ACCESS_TOKEN.
"""
import argparse
import getpass
import sys
import time
from auth import MIN_PASSWORD, USERNAME, UserStore, hash_password


def ask_password():
    while True:
        first = getpass.getpass('Mật khẩu mới: ')
        if len(first) < MIN_PASSWORD:
            print(f'Mật khẩu cần ít nhất {MIN_PASSWORD} ký tự.')
        elif first != getpass.getpass('Nhập lại: '):
            print('Hai lần nhập không khớp.')
        else:
            return first


def main(argv=None):
    parser = argparse.ArgumentParser(description='Quản lý tài khoản bác sĩ của web Fe2Gen.')
    parser.add_argument('action', choices=['add', 'passwd', 'disable', 'enable', 'remove', 'list'])
    parser.add_argument('username', nargs='?')
    parser.add_argument('--name', help='Tên hiển thị, ví dụ "BS. Nguyễn Văn An"')
    parser.add_argument('--file', help='Đường dẫn users.json (mặc định WEB_USERS_FILE hoặc ~/prenatal_pubcasefinder/.deployment/users.json)')
    args = parser.parse_args(argv)
    store = UserStore(args.file)
    users = store.all()
    if args.action == 'list':
        print(f'{store.path}: {len(users)} tài khoản')
        for name, u in sorted(users.items()):
            print(f"  {name:<20} {u.get('name', ''):<30} {'ĐÃ KHÓA' if u.get('disabled') else 'hoạt động'}")
        return 0
    username = (args.username or '').strip().lower()
    if not USERNAME.match(username):
        parser.error('Tên đăng nhập gồm 3–32 ký tự a-z, 0-9, dấu chấm, gạch ngang hoặc gạch dưới.')
    if args.action == 'add':
        if username in users:
            parser.error('Tài khoản đã có; dùng passwd để đổi mật khẩu.')
        users[username] = {'name': args.name or username, 'password': hash_password(ask_password()),
                           'disabled': False, 'created': time.strftime('%Y-%m-%d %H:%M')}
    elif username not in users:
        parser.error('Không có tài khoản ' + username)
    elif args.action == 'passwd':
        users[username]['password'] = hash_password(ask_password())
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
