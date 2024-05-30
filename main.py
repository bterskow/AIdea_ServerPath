from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from config import Config
from openai import OpenAI
import uvicorn, aiohttp, json, jwt, boto3

app = FastAPI(docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_headers=['*'], allow_methods=['*'], allow_origins=['*'])

config = Config()
secret_key = config.secret_key
dynamodb_client = boto3.resource('dynamodb', 
    aws_access_key_id=config.dynamodb_aws_access_key_id, 
    aws_secret_access_key=config.dynamodb_aws_secret_access_key,
    region_name= config.dynamodb_region_name
)
dynamodb_table = dynamodb_client.Table(config.dynamodb_table_name)
dynamodb_table_feedbacks = dynamodb_client.Table(config.dynamodb_table_feedbacks)
gpt_client = OpenAI(api_key=config.gpt_secret_key)

def __decode_token(token):
    data = jwt.decode(token, secret_key, algorithms=['HS256'])
    return data

def __encode_data(data):
    token = jwt.encode(data, secret_key, algorithm='HS256')
    return token

async def __generate_answer_from_ai(card_desc):
    response = {'status': 'error', 'err_description': '', 'response': {}}

    try:
        ai_response = gpt_client.chat.completions.create(
            model="gpt-3.5-turbo-0125",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a helpful it-assistant designed to output JSON."},
                {"role": "user", "content": f"Do you think this idea suggested by a coworker is a good one: '{card_desc}'? Rate it on a 10 point scale and explain your decision."}
            ]
        )

        response['response'] = ai_response.choices[0].message.content
        response['status'] = 'success'

    except Exception as e:
        response['err_description'] = str(e)

    return response

# async def __send_notification_to_telegram(text, bot_token, chat_id):
#     response = {'status': 'error', 'err_description': ''}

#     try:
#         async with aiohttp.ClientSession() as session:
#             async with session.get(
#                 f'https://api.telegram.org/bot{bot_token}/sendMessage',
#                 params={
#                     'chat_id': chat_id,
#                     'text': text,
#                     'parse_mode': 'HTML'
#                 }
#             ) as request:
#                 request_json = await request.json()
#                 if not request_json.get('ok', False):
#                     response['err_description'] = request_json['description']
#                     return response

#         response['status'] = 'success'

#     except Exception as e:
#         response['err_description'] = str(e)

#     return response

async def __save_feedback(webhook_id, feedback_data):
    response = {'status': 'error', 'err_description': ''}

    try:
        token = __encode_data(feedback_data)

        get_item = dynamodb_table_feedbacks.get_item(
                Key={
                    'webhook_id': webhook_id,
                }
            )

        tokens = get_item.get('Item', {}).get('tokens')
        if tokens is not None:
            tokens = json.loads(tokens)
            tokens.append(token)

            dynamodb_table_feedbacks.update_item(
                Key={
                    'webhook_id': webhook_id,
                },
                AttributeUpdates={
                    'tokens': {
                        'Value': json.dumps(tokens),
                        'Action': 'PUT'
                    }
                }
            )
        
        else:
            dynamodb_table_feedbacks.put_item(
                Item={
                    'webhook_id': webhook_id,
                    'tokens': json.dumps([token])
                }
            )
        
        response['status'] = 'success'

    except Exception as e:
        response['err_description'] = str(e)

    return response

async def __card_info(card_id, trello_api_key, trello_api_token):
    response = {'status': 'error', 'err_description': '', 'continue': None}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'https://api.trello.com/1/cards/{card_id}?key={trello_api_key}&token={trello_api_token}') as request:
                request_json = await request.json()

                cover = request_json.get('cover', {})
                color = cover.get('color')

                if color in ['red', 'green']:
                    response['continue'] = False
                else:
                    response['continue'] = True

                response['status'] = 'success'

    except Exception as e:
        response['err_description'] = str(e)

    return response

@app.api_route('/trello/proposal', name='Proposal', description='Receive the proposed innovation from the Trello board, evaluate it with its AI and pass it to the teamleader', status_code=200, methods=['HEAD', 'POST'])
async def proposal(request: Request):
    response = {'status': 'error', 'err_description': ''}

    try:
        request_json = await request.json()

        webhook_id = request_json.get('webhook', {}).get('id')
        if webhook_id is None:
            logger.error('Webhook id not found!')
            response['err_description'] = 'Webhook id not found!'
            return response

        get_item = dynamodb_table.get_item(Key={'webhook_id': webhook_id})
        token = get_item.get('Item', {}).get('token')
        if token is None:
            logger.error('Token not found in remote db!')
            response['err_description'] = 'Token not found in remote db!'
            return response

        project_data = __decode_token(token)

        action = request_json.get('action', {})
        data = action.get('data', {})

        card = data.get('card', {})
        card_id = card.get('id')
        card_info = await __card_info(card_id, project_data['trello_api_key'], project_data['trello_api_token'])
        logger.debug(card_info)

        card_info_status = card_info['status']
        card_info_err_description = card_info['err_description']
        card_info_continue = card_info['continue']

        if card_info_status == 'error':
            logger.error(card_info_err_description)
            response['err_description'] = card_info_err_description
            return JSONResponse(content=response)

        if card_info_continue == True:
            card_desc = card.get('desc')
            if all(value is not None and len(value) > 0 for value in [card_desc]):
                generate_answer_from_ai = await __generate_answer_from_ai(card_desc)
                logger.info(generate_answer_from_ai)

                generate_answer_from_ai_status = generate_answer_from_ai['status']
                generate_answer_from_ai_err_description = generate_answer_from_ai['err_description']
                generate_answer_from_ai_response = generate_answer_from_ai['response']

                if generate_answer_from_ai_status == 'error':
                    logger.error(generate_answer_from_ai_err_description)
                    response['err_description'] = generate_answer_from_ai_err_description
                    return JSONResponse(content=response)

                generate_answer_from_ai_response = json.loads(generate_answer_from_ai_response)
                rating = generate_answer_from_ai_response.get('rating')
                explanation = generate_answer_from_ai_response.get('explanation')
                response_example = json.dumps({
                    "card_id": card_id,
                    "result_mark": ""
                })

#                 text = f'''
# <b>Proposal</b>: {card_desc}
# <b>Rating</b>: {rating}
# <b>AI mind</b>: {explanation}
# <b>Card ID</b>: {card_id}

# <b>Response example</b>: {response_example}
# <b>Marks</b>: 🟢 or 🔴
#                 '''

                feedback_data = {
                    'card_desc': card_desc,
                    'rating': rating,
                    'explanation': explanation,
                    'card_id': card_id,
                    'trello_api_key': project_data['trello_api_key'],
                    'trello_api_token': project_data['trello_api_token'],
                    'result_mark': '🟢 or 🔴'
                }

                save_feedback = await __save_feedback(webhook_id, feedback_data)
                logger.info(save_feedback)
                save_feedback_status = save_feedback['status']
                save_feedback_err_description = save_feedback['err_description']

                if save_feedback_status == 'error':
                    logger.error(save_feedback_err_description)
                    response['err_description'] = save_feedback_err_description
                    return JSONResponse(content=response)

                # send_notification_to_telegram = await __send_notification_to_telegram(text, project_data['bot_token'], project_data['chat_id'])
                # logger.info(send_notification_to_telegram)

                # send_notification_to_telegram_status = send_notification_to_telegram['status']
                # send_notification_to_telegram_err_description = send_notification_to_telegram['err_description']

                # if send_notification_to_telegram_status == 'error':
                #     logger.error(send_notification_to_telegram_err_description)
                #     response['err_description'] = send_notification_to_telegram_err_description
                #     return JSONResponse(content=response)
            else:
                logger.debug('Invalid card description!')

        response['status'] = 'success'

    except Exception as e:
        response['err_description'] = str(e)

    return JSONResponse(content=response)

if __name__ == '__main__':
    uvicorn.run('main:app', host='127.0.0.1', port=8000, reload=True)
