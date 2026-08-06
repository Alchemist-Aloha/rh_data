import getpass
import os
import pickle
import secrets
import time

from robin_stocks.robinhood.helper import request_post, request_get, update_session, set_login_state, get_output
from robin_stocks.robinhood.urls import login_url, challenge_url, positions_url

def generate_device_token():
    """Generates a cryptographically secure device token."""
    rands = [secrets.randbelow(256) for _ in range(16)]
    hexa = [str(hex(i + 256)).lstrip("0x")[1:] for i in range(256)]
    token = ""
    for i, r in enumerate(rands):
        token += hexa[r]
        if i in [3, 5, 7, 9]:
            token += "-"
    return token

def respond_to_challenge(challenge_id, sms_code):
    """This function will post to the challenge url.
    :param challenge_id: The challenge id.
    :type challenge_id: str
    :param sms_code: The sms code.
    :type sms_code: str
    :returns:  The response from requests.
    """
    url = challenge_url(challenge_id)
    payload = {
        'response': sms_code
    }
    return(request_post(url, payload))

def login(username=None, password=None, expiresIn=689285, scope='internal', by_sms=True, store_session=True, mfa_code=None, pickle_path="", pickle_name="") -> dict:
    """This function will effectively log the user into robinhood by getting an
    authentication token and saving it to the session header. By default, it
    will store the authentication token in a pickle file and load that value
    on subsequent logins.
    :param username: The username for your robinhood account, usually your email.
        Not required if credentials are already cached and valid.
    :type username: Optional[str]
    :param password: The password for your robinhood account. Not required if
        credentials are already cached and valid.
    :type password: Optional[str]
    :param expiresIn: The time until your login session expires. This is in seconds.
    :type expiresIn: Optional[int]
    :param scope: Specifies the scope of the authentication.
    :type scope: Optional[str]
    :param by_sms: Specifies whether to send an email(False) or an sms(True)
    :type by_sms: Optional[boolean]
    :param store_session: Specifies whether to save the log in authorization
        for future log ins.
    :type store_session: Optional[boolean]
    :param mfa_code: MFA token if enabled.
    :type mfa_code: Optional[str]
    :param pickle_path: Allows users to specify the path of the pickle file.
        Accepts both relative and absolute paths.
    :param pickle_name: Allows users to name Pickle token file in order to switch
        between different accounts without having to re-login every time.
    :returns:  A dictionary with log in information. The 'access_token' keyword contains the access token, and the 'detail' keyword \
    contains information on whether the access token was generated or loaded from pickle file.
    """
    device_token = generate_device_token()
    home_dir = os.path.expanduser("~")
    data_dir = os.path.join(home_dir, ".tokens")
    if pickle_path:
        if not os.path.isabs(pickle_path):
            # normalize relative paths
            pickle_path = os.path.normpath(os.path.join(os.getcwd(), pickle_path))
        data_dir = pickle_path
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
    creds_file = "robinhood" + pickle_name + ".pickle"
    pickle_path = os.path.join(data_dir, creds_file)
    # Challenge type is used if not logging in with two-factor authentication.
    if by_sms:
        challenge_type = "sms"
    else:
        challenge_type = "email"
    url = login_url()
    login_payload = {
        'client_id': 'c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS',
        'expires_in': expiresIn,
        'grant_type': 'password',
        'password': password,
        'scope': scope,
        'username': username,
        'challenge_type': challenge_type,
        'device_token': device_token,
        'try_passkeys': False,
        'token_request_path':'/login',
        'create_read_only_secondary_token':True,
    }
    if mfa_code:
        login_payload['mfa_code'] = mfa_code
    # If authentication has been stored in pickle file then load it. Stops login server from being pinged so much.
    if os.path.isfile(pickle_path):
        # If store_session has been set to false then delete the pickle file, otherwise try to load it.
        # Loading pickle file will fail if the acess_token has expired.
        if store_session:
            try:
                with open(pickle_path, 'rb') as f:
                    pickle_data = pickle.load(f)
                    access_token = pickle_data['access_token']
                    token_type = pickle_data['token_type']
                    refresh_token = pickle_data['refresh_token']
                    # Set device_token to be the original device token when first logged in.
                    pickle_device_token = pickle_data['device_token']
                    login_payload['device_token'] = pickle_device_token
                    # Set login status to True in order to try and get account info.
                    set_login_state(True)
                    update_session(
                        'Authorization', '{0} {1}'.format(token_type, access_token))
                    # Try to load account profile to check that authorization token is still valid.
                    res = request_get(
                        positions_url(), 'pagination', {'nonzero': 'true'}, jsonify_data=False)
                    # Raises exception if response code is not 200.
                    res.raise_for_status()
                    return({'access_token': access_token, 'token_type': token_type,
                            'expires_in': expiresIn, 'scope': scope, 'detail': 'logged in using authentication in {0}'.format(creds_file),
                            'backup_code': None, 'refresh_token': refresh_token})
            except Exception:
                print(
                    "ERROR: There was an issue loading pickle file. Authentication may be expired - logging in normally.", file=get_output())
                set_login_state(False)
                update_session('Authorization', None)
        else:
            os.remove(pickle_path)
    # Try to log in normally.
    if not username:
        username = input("Robinhood username: ")
        login_payload['username'] = username
    if not password:
        password = getpass.getpass("Robinhood password: ")
        login_payload['password'] = password
    data = request_post(url, login_payload)
    # Handle case where mfa or challenge is required.
    if data:
        if 'mfa_required' in data:
            mfa_token = input("Please type in the MFA code: ")
            login_payload['mfa_code'] = mfa_token
            res = request_post(url, login_payload, jsonify_data=False)
            while (res.status_code != 200):
                mfa_token = input(
                    "That MFA code was not correct. Please type in another MFA code: ")
                login_payload['mfa_code'] = mfa_token
                res = request_post(url, login_payload, jsonify_data=False)
            data = res.json()
        elif 'challenge' in data:
            challenge_id = data['challenge']['id']
            sms_code = input('Enter Robinhood code for validation: ')
            res = respond_to_challenge(challenge_id, sms_code)
            while 'challenge' in res and res['challenge']['remaining_attempts'] > 0:
                sms_code = input('That code was not correct. {0} tries remaining. Please type in another code: '.format(
                    res['challenge']['remaining_attempts']))
                res = respond_to_challenge(challenge_id, sms_code)
            update_session(
                'X-ROBINHOOD-CHALLENGE-RESPONSE-ID', challenge_id)
            data = request_post(url, login_payload)
        elif 'verification_workflow' in data:
            print("Verification workflow required. Please check your Robinhood app for instructions.")
            workflow_id = data['verification_workflow']['id']
            workflow_mfa_code = mfa_code if isinstance(mfa_code, str) else input("Enter the MFA code: ")
            _validate_sherrif_id(device_token=device_token, workflow_id=workflow_id, mfa_code=workflow_mfa_code)
            data = request_post(url, login_payload)
        # Update Session data with authorization or raise exception with the information present in data.
        if 'access_token' in data:
            token = '{0} {1}'.format(data['token_type'], data['access_token'])
            update_session('Authorization', token)
            set_login_state(True)
            data['detail'] = "logged in with brand new authentication code."
            if store_session:
                with open(pickle_path, 'wb') as f:
                    pickle.dump({'token_type': data['token_type'],
                                 'access_token': data['access_token'],
                                 'refresh_token': data['refresh_token'],
                                 'device_token': login_payload['device_token']}, f)
        else:
            if 'detail' in data:
                raise Exception(data['detail'])
            raise Exception(f"Received an error response {data}")
    else:
        raise Exception('Error: Trouble connecting to robinhood API. Check internet connection.')
    return(data)

def _validate_sherrif_id(device_token:str, workflow_id:str,mfa_code:str):
    url = "https://api.robinhood.com/pathfinder/user_machine/"
    machine_payload = {
        'device_id': device_token,
        'flow': 'suv',
        'input': {'workflow_id': workflow_id}
    }
    data = request_post(url=url, payload=machine_payload,json=True)
    machine_id = _get_sherrif_challenge(data)
    inquiries_url = f"https://api.robinhood.com/pathfinder/inquiries/{machine_id}/user_view/"
    response = request_get(inquiries_url)
    challenge_id = response["context"]["sheriff_challenge"]["id"] # used to be type_context
    challenge_url = f"https://api.robinhood.com/challenge/{challenge_id}/respond/" 
    challenge_payload = {'response': mfa_code}
    request_post(url=challenge_url, payload=challenge_payload)
    start_time = time.time()
    while time.time() - start_time < 60: # 1 minute
        time.sleep(5)
        email_text_code = input("Prompt for text or email code (if prompt sent via robinhood app set this to 0000 after verifying):")
        challenge_payload['response'] = email_text_code
        request_post(url=challenge_url, payload=challenge_payload)
        inquiries_payload = {"sequence":0,"user_input":{"status":"continue"}}
        inquiries_response = request_post(url=inquiries_url, payload=inquiries_payload,json=True)
        if inquiries_response["type_context"]["result"] == "workflow_status_approved":
            print("login successful")
            return
        else:
            raise Exception("workflow status not approved")
    raise Exception("Login confirmation timed out. Please try again.")


def _get_sherrif_challenge(data):
    if "id" in data:
        return data["id"]
    raise Exception("Id not returned in user-machine call")


