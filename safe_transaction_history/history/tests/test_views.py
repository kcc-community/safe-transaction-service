import datetime
import logging

from django.urls import reverse
from django_eth.constants import NULL_ADDRESS
from hexbytes import HexBytes
from rest_framework import status
from rest_framework.test import APITestCase

from gnosis.safe.safe_service import SafeOperation
from gnosis.safe.tests.factories import get_eth_address_with_key
from gnosis.safe.tests.safe_test_case import TestCaseWithSafeContractMixin

from ..models import MultisigConfirmation, MultisigTransaction
from ..serializers import SafeMultisigTransactionHistorySerializer
from .factories import (MultisigTransactionConfirmationFactory,
                        MultisigTransactionFactory,
                        generate_multisig_transactions)

logger = logging.getLogger(__name__)


class TestHistoryViews(APITestCase, TestCaseWithSafeContractMixin):

    operation = 0
    WITHDRAW_AMOUNT = 50000000000000000

    @classmethod
    def setUpTestData(cls):
        cls.prepare_safe_tests()

    def test_about(self):
        request = self.client.get(reverse('v1:about'))
        self.assertEqual(request.status_code, status.HTTP_200_OK)

    def test_multisig_transaction_creation_flow(self):
        safe_address, safe_instance, owners, funder, initial_funding_wei, _ = self.deploy_test_safe()

        balance = self.w3.eth.getBalance(safe_address)
        self.assertEqual(initial_funding_wei, balance)

        to, _ = get_eth_address_with_key()
        value = self.WITHDRAW_AMOUNT
        data = b''
        operation = SafeOperation.CALL.value
        safe_tx_gas = 500000
        data_gas = 500000
        gas_price = 1
        gas_token = NULL_ADDRESS
        refund_receiver = NULL_ADDRESS
        nonce = 0

        safe_tx_hash = self.safe_service.get_hash_for_safe_tx(safe_address, to, value, data, operation, safe_tx_gas,
                                                              data_gas, gas_price, gas_token, refund_receiver, nonce)

        safe_tx_contract_hash = safe_instance.functions.getTransactionHash(to, value, data, operation,
                                                                           safe_tx_gas, data_gas, gas_price, gas_token,
                                                                           refund_receiver, nonce).call()

        self.assertEqual(safe_tx_hash, safe_tx_contract_hash)

        sender = owners[0]
        tx_hash_owner0 = safe_instance.functions.approveHash(safe_tx_hash).transact({'from': sender})
        is_approved = safe_instance.functions.approvedHashes(sender, safe_tx_hash).call()
        self.assertTrue(is_approved)

        transaction_data = {
            'safe': safe_address,
            'to': to,
            'value': value,
            'data': b''.hex(),
            'operation': operation,
            'nonce': nonce,
            'safe_tx_gas': safe_tx_gas,
            'data_gas': data_gas,
            'gas_price': gas_price,
            'contract_transaction_hash': safe_tx_contract_hash.hex(),
            'transaction_hash': tx_hash_owner0.hex(),
            'block_number': 0,
            'block_date_time': datetime.datetime.now(),
            'sender': sender,
            'type': 'confirmation'
        }

        serializer = SafeMultisigTransactionHistorySerializer(data=transaction_data)
        self.assertTrue(serializer.is_valid())

        # Save
        request = self.client.post(reverse('v1:multisig-transactions', kwargs={'address': safe_address}),
                                   data=serializer.data, format='json')
        self.assertEqual(request.status_code, status.HTTP_202_ACCEPTED)

        db_multisig_transactions = MultisigTransaction.objects.filter(safe=safe_address,
                                                                      to=to,
                                                                      value=self.WITHDRAW_AMOUNT,
                                                                      data=None,
                                                                      operation=SafeOperation.CALL.value,
                                                                      nonce=nonce)

        self.assertEqual(db_multisig_transactions.count(), 1)

        # Send Tx signed by owner 1
        sender = owners[1]
        tx_hash_owner1 = safe_instance.functions.approveHash(safe_tx_hash).transact({'from': sender})
        is_approved = safe_instance.functions.approvedHashes(sender, safe_tx_hash).call()
        self.assertTrue(is_approved)

        # Send confirmation from owner1 to API
        transaction_data = {
            'safe': safe_address,
            'to': to,
            'value': value,
            'data': b''.hex(),
            'operation': operation,
            'nonce': nonce,
            'safe_tx_gas': safe_tx_gas,
            'data_gas': data_gas,
            'gas_price': gas_price,
            'contract_transaction_hash': safe_tx_contract_hash.hex(),
            'transaction_hash': tx_hash_owner1.hex(),
            'block_number': 0,
            'block_date_time': datetime.datetime.now(),
            'sender': sender,
            'type': 'confirmation'
        }

        serializer = SafeMultisigTransactionHistorySerializer(data=transaction_data)
        self.assertTrue(serializer.is_valid())

        # Save
        request = self.client.post(reverse('v1:multisig-transactions', kwargs={'address': safe_address}),
                                   data=serializer.data, format='json')
        self.assertEqual(request.status_code, status.HTTP_202_ACCEPTED)

        # v == 1, r = owner -> Signed previously
        signatures = self.safe_service.signatures_to_bytes([(1, int(owner, 16), 0)
                                                            for owner in
                                                            sorted(owners[:2], key=lambda x: x.lower())])

        # Execute Multisig Transaction
        tx_execute_hash, _ = self.safe_service.send_multisig_tx(safe_address, to, value, data, operation,
                                                                safe_tx_gas, data_gas, gas_price, gas_token,
                                                                refund_receiver, signatures)

        is_executed = self.safe_service.retrieve_nonce(safe_address) == (nonce + 1)
        self.assertTrue(is_executed)

        # Send confirmation from owner2 to API
        transaction_data = {
            'safe': safe_address,
            'to': to,
            'value': value,
            'data': b''.hex(),
            'operation': operation,
            'nonce': nonce,
            'safe_tx_gas': safe_tx_gas,
            'data_gas': data_gas,
            'gas_price': gas_price,
            'contract_transaction_hash': safe_tx_contract_hash.hex(),
            'transaction_hash': tx_execute_hash.hex(),
            'block_number': 0,
            'block_date_time': datetime.datetime.now(),
            'sender': owners[0],
            'type': 'execution'
        }

        serializer = SafeMultisigTransactionHistorySerializer(data=transaction_data)
        self.assertTrue(serializer.is_valid())

        # Save
        request = self.client.post(reverse('v1:multisig-transactions', kwargs={'address': safe_address}),
                                   data=serializer.data, format='json')
        self.assertEqual(request.status_code, status.HTTP_202_ACCEPTED)

        balance = self.w3.eth.getBalance(to)
        self.assertEqual(balance, value)

        # Get multisig transaction data
        request = self.client.get(reverse('v1:multisig-transactions', kwargs={'address': safe_address}),
                                  format='json')
        self.assertEqual(request.status_code, status.HTTP_200_OK)
        self.assertEqual(len(request.json()['results']), 1)
        self.assertEqual(len(request.json()['results'][0]['confirmations']), 3)
        self.assertEqual(request.json()['results'][0]['confirmations'][2]['owner'],
                         owners[0])  # confirmations are sorted by creation date DESC
        self.assertEqual(request.json()['results'][0]['confirmations'][2]['type'], 'CONFIRMATION')
        self.assertEqual(request.json()['results'][0]['confirmations'][0]['type'], 'EXECUTION')

    def test_create_multisig_invalid_transaction_parameters(self):
        safe_address, safe_instance, owners, funder, initial_funding_wei, _ = self.deploy_test_safe()
        self.assertIsNotNone(safe_address)
        safe_nonce = self.safe_service.retrieve_nonce(safe_address)
        self.assertEqual(safe_nonce, 0)

        to, _ = get_eth_address_with_key()
        value = self.WITHDRAW_AMOUNT
        data = b''
        operation = SafeOperation.CALL.value
        safe_tx_gas = 500000
        data_gas = 500000
        gas_price = 1
        gas_token = NULL_ADDRESS
        refund_receiver = NULL_ADDRESS
        nonce = 0
        safe_tx_hash = self.safe_service.get_hash_for_safe_tx(safe_address, to, value, data, operation, safe_tx_gas,
                                                              data_gas, gas_price, gas_token, refund_receiver, nonce)

        sender = owners[0]
        tx_hash_owner0 = safe_instance.functions.approveHash(safe_tx_hash).transact({'from': sender})
        is_approved = safe_instance.functions.approvedHashes(sender, safe_tx_hash).call()
        self.assertTrue(is_approved)

        # Call API with invalid contract_transaction_hash sent by owner1 to API
        transaction_data = {
            'safe': safe_address,
            'to': to,
            'value': value,
            'data': b''.hex(),
            'operation': operation,
            'nonce': nonce,
            'safe_tx_gas': safe_tx_gas,
            'data_gas': data_gas,
            'gas_price': gas_price,
            'contract_transaction_hash': safe_tx_hash.hex()[:-2],   # invalid contract_transaction_hash
            'transaction_hash': tx_hash_owner0.hex(),
            'block_number': 0,
            'block_date_time': datetime.datetime.now(),
            'sender': sender,
            'type': 'confirmation'
        }

        request = self.client.post(reverse('v1:multisig-transactions', kwargs={'address': safe_address}),
                                   data=transaction_data, format='json')
        self.assertEqual(request.status_code, status.HTTP_400_BAD_REQUEST)

        # Call API with invalid 'type' property
        transaction_data = {
            'safe': safe_address,
            'to': to,
            'value': value,
            'data': b''.hex(),
            'operation': operation,
            'nonce': nonce,
            'safe_tx_gas': safe_tx_gas,
            'data_gas': data_gas,
            'gas_price': gas_price,
            'contract_transaction_hash': safe_tx_hash.hex(),
            'transaction_hash': tx_hash_owner0.hex(),
            'block_number': 0,
            'block_date_time': datetime.datetime.now(),
            'sender': sender,
            'type': 'wrong_type'
        }

        request = self.client.post(reverse('v1:multisig-transactions', kwargs={'address': safe_address}),
                                   data=transaction_data, format='json')
        self.assertEqual(request.status_code, status.HTTP_400_BAD_REQUEST)

        # Use correct contract_transaction_hash
        transaction_data = {
            'safe': safe_address,
            'to': to,
            'value': value,
            'data': b''.hex(),
            'operation': operation,
            'nonce': nonce,
            'safe_tx_gas': safe_tx_gas,
            'data_gas': data_gas,
            'gas_price': gas_price,
            'contract_transaction_hash': safe_tx_hash.hex(),
            'transaction_hash': tx_hash_owner0.hex(),
            'block_number': 0,
            'block_date_time': datetime.datetime.now(),
            'sender': sender,
            'type': 'confirmation'
        }

        # Create wrong safe address
        wrong_safe_address = safe_address[:-5] + 'fffff'  # not checksumed address

        request = self.client.post(reverse('v1:multisig-transactions', kwargs={'address': wrong_safe_address}),
                                   data=transaction_data, format='json')
        self.assertEqual(request.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

        with self.assertRaises(MultisigTransaction.DoesNotExist):
            MultisigTransaction.objects.get(safe=safe_address, nonce=safe_nonce)

        with self.assertRaises(MultisigConfirmation.DoesNotExist):
            MultisigConfirmation.objects.get(owner=owners[0], contract_transaction_hash=safe_tx_hash.hex())

        # Create invalid not base16 address
        wrong_safe_address = safe_address[:-4] + 'test'  # not base16 address
        request = self.client.post(reverse('v1:multisig-transactions', kwargs={'address': wrong_safe_address}),
                                   data=transaction_data, format='json')
        self.assertEqual(request.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

        with self.assertRaises(MultisigTransaction.DoesNotExist):
            MultisigTransaction.objects.get(safe=safe_address, nonce=safe_nonce)

        with self.assertRaises(MultisigConfirmation.DoesNotExist):
            MultisigConfirmation.objects.get(owner=owners[0], contract_transaction_hash=safe_tx_hash.hex())

        # Call API using wrong sender (owner1), which has not been approved yet
        transaction_data = {
            'safe': safe_address,
            'to': to,
            'value': value,
            'data': b''.hex(),
            'operation': operation,
            'nonce': nonce,
            'safe_tx_gas': safe_tx_gas,
            'data_gas': data_gas,
            'gas_price': gas_price,
            'contract_transaction_hash': safe_tx_hash.hex(),
            'transaction_hash': tx_hash_owner0.hex(),
            'block_number': 0,
            'block_date_time': datetime.datetime.now(),
            'sender': owners[1],
            'type': 'confirmation'
        }

        request = self.client.post(reverse('v1:multisig-transactions', kwargs={'address': safe_address}),
                                   data=transaction_data, format='json')
        self.assertEqual(request.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

        with self.assertRaises(MultisigTransaction.DoesNotExist):
            MultisigTransaction.objects.get(safe=safe_address, nonce=safe_nonce)

        with self.assertRaises(MultisigConfirmation.DoesNotExist):
            MultisigConfirmation.objects.get(owner=owners[0], contract_transaction_hash=safe_tx_hash.hex())
            MultisigConfirmation.objects.get(owner=owners[1], contract_transaction_hash=safe_tx_hash.hex())

        # Call API using invalid sender address
        transaction_data = {
            'safe': safe_address,
            'to': to,
            'value': value,
            'data': b''.hex(),
            'operation': operation,
            'nonce': nonce,
            'safe_tx_gas': safe_tx_gas,
            'data_gas': data_gas,
            'gas_price': gas_price,
            'contract_transaction_hash': safe_tx_hash.hex(),
            'transaction_hash': tx_hash_owner0.hex(),
            'block_number': 0,
            'block_date_time': datetime.datetime.now(),
            'sender': owners[0][:-5] + 'fffff',
            'type': 'confirmation'
        }
        request = self.client.post(reverse('v1:multisig-transactions', kwargs={'address': safe_address}),
                                   data=transaction_data, format='json')
        self.assertEqual(request.status_code, status.HTTP_400_BAD_REQUEST)
        with self.assertRaises(MultisigTransaction.DoesNotExist):
            MultisigTransaction.objects.get(safe=safe_address, nonce=safe_nonce)

        with self.assertRaises(MultisigConfirmation.DoesNotExist):
            MultisigConfirmation.objects.get(owner=owners[0], contract_transaction_hash=safe_tx_hash.hex())
            MultisigConfirmation.objects.get(owner=owners[1], contract_transaction_hash=safe_tx_hash.hex())

        # Call API using invalid 'to' address
        transaction_data = {
            'safe': safe_address,
            'to': owners[0][:-5] + 'fffff',
            'value': value,
            'data': b''.hex(),
            'operation': operation,
            'nonce': nonce,
            'safe_tx_gas': safe_tx_gas,
            'data_gas': data_gas,
            'gas_price': gas_price,
            'contract_transaction_hash': safe_tx_hash.hex(),
            'transaction_hash': tx_hash_owner0.hex(),
            'block_number': 0,
            'block_date_time': datetime.datetime.now(),
            'sender': sender,
            'type': 'confirmation'
        }
        request = self.client.post(reverse('v1:multisig-transactions', kwargs={'address': safe_address}),
                                   data=transaction_data, format='json')
        self.assertEqual(request.status_code, status.HTTP_400_BAD_REQUEST)
        with self.assertRaises(MultisigTransaction.DoesNotExist):
            MultisigTransaction.objects.get(safe=safe_address, nonce=safe_nonce)

        with self.assertRaises(MultisigConfirmation.DoesNotExist):
            MultisigConfirmation.objects.get(owner=owners[0], contract_transaction_hash=safe_tx_hash.hex())
            MultisigConfirmation.objects.get(owner=owners[1], contract_transaction_hash=safe_tx_hash.hex())

        # Call API with correct data values and parameters
        transaction_data = {
            'safe': safe_address,
            'to': to,
            'value': value,
            'data': b''.hex(),
            'operation': operation,
            'nonce': nonce,
            'safe_tx_gas': safe_tx_gas,
            'data_gas': data_gas,
            'gas_price': gas_price,
            'contract_transaction_hash': safe_tx_hash.hex(),
            'transaction_hash': tx_hash_owner0.hex(),
            'block_number': 0,
            'block_date_time': datetime.datetime.now(),
            'sender': sender,
            'type': 'confirmation'
        }
        request = self.client.post(reverse('v1:multisig-transactions', kwargs={'address': safe_address}),
                                   data=transaction_data, format='json')
        self.assertEqual(request.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(MultisigTransaction.objects.filter(safe=safe_address, nonce=safe_nonce).count(), 1)
        self.assertEqual(MultisigConfirmation.objects.filter(
            owner=owners[0], contract_transaction_hash=safe_tx_hash.hex()).count(), 1)

    def test_create_multisig_invalid_owner(self):
        safe_address, safe_instance, owners, funder, initial_funding_wei, _ = self.deploy_test_safe()
        self.assertIsNotNone(safe_address)
        safe_nonce = self.safe_service.retrieve_nonce(safe_address)
        self.assertEqual(safe_nonce, 0)

        to, _ = get_eth_address_with_key()
        value = self.WITHDRAW_AMOUNT
        data = b''
        operation = SafeOperation.CALL.value
        safe_tx_gas = 500000
        data_gas = 500000
        gas_price = 1
        gas_token = NULL_ADDRESS
        refund_receiver = NULL_ADDRESS
        nonce = 0
        safe_tx_hash = self.safe_service.get_hash_for_safe_tx(safe_address, to, value, data, operation, safe_tx_gas,
                                                              data_gas, gas_price, gas_token, refund_receiver, nonce)

        sender = owners[0]
        tx_hash_owner0 = safe_instance.functions.approveHash(safe_tx_hash).transact({'from': sender})
        is_approved = safe_instance.functions.approvedHashes(sender, safe_tx_hash).call()
        self.assertTrue(is_approved)

        # Send confirmation from owner1 to API
        transaction_data = {
            'safe': safe_address,
            'to': to,
            'value': value,
            'data': b''.hex(),
            'operation': operation,
            'nonce': nonce,
            'safe_tx_gas': safe_tx_gas,
            'data_gas': data_gas,
            'gas_price': gas_price,
            'contract_transaction_hash': safe_tx_hash.hex()[:-2],  # invalid contract_transaction_hash
            'transaction_hash': tx_hash_owner0.hex(),
            'block_number': 0,
            'block_date_time': datetime.datetime.now(),
            'sender': sender,
            'type': 'confirmation'
        }

        serializer = SafeMultisigTransactionHistorySerializer(data=transaction_data)
        self.assertFalse(serializer.is_valid())

        transaction_data['contract_transaction_hash'] = safe_tx_hash.hex()
        serializer = SafeMultisigTransactionHistorySerializer(data=transaction_data)
        self.assertTrue(serializer.is_valid())

    def test_get_multisig_transactions(self):
        safe_address, safe_instance, owners, funder, initial_funding_wei, _ = self.deploy_test_safe()

        request = self.client.get(reverse('v1:multisig-transactions', kwargs={'address': safe_address}),
                                  format='json')
        self.assertEqual(request.status_code, status.HTTP_404_NOT_FOUND)

        multisig_transaction_instance = MultisigTransactionFactory()
        request = self.client.get(reverse('v1:multisig-transactions',
                                          kwargs={'address': multisig_transaction_instance.safe}),
                                  format='json')
        self.assertEqual(request.status_code, status.HTTP_200_OK)
        self.assertEqual(len(request.json()['results']), 1)
        self.assertEqual(len(request.json()['results'][0]['confirmations']), 0)

        multisig_confirmation_instance = MultisigTransactionConfirmationFactory(
            multisig_transaction=multisig_transaction_instance)
        request = self.client.get(reverse('v1:multisig-transactions',
                                          kwargs={'address': multisig_confirmation_instance.multisig_transaction.safe}),
                                  format='json')
        self.assertEqual(request.status_code, status.HTTP_200_OK)
        self.assertEqual(len(request.json()['results']), 1)
        self.assertEqual(len(request.json()['results'][0]['confirmations']), 1)

        # Filter by owners
        multisig_confirmation_instance = MultisigTransactionConfirmationFactory(
            multisig_transaction=multisig_transaction_instance, owner=owners[0])

        query_string = '?owners=' + owners[0]
        request = self.client.get(reverse('v1:multisig-transactions',
                                          kwargs={'address': multisig_confirmation_instance.multisig_transaction.safe})
                                  + query_string, format='json')
        self.assertEqual(request.status_code, status.HTTP_200_OK)
        self.assertEqual(len(request.json()['results']), 1)
        self.assertEqual(len(request.json()['results'][0]['confirmations']), 1)

        query_string = '?owners=%s,%s' % (owners[0], owners[1])
        request = self.client.get(reverse('v1:multisig-transactions',
                                          kwargs={'address': multisig_confirmation_instance.multisig_transaction.safe})
                                  + query_string, format='json')
        self.assertEqual(request.status_code, status.HTTP_200_OK)
        self.assertEqual(len(request.json()['results']), 1)
        self.assertEqual(len(request.json()['results'][0]['confirmations']), 1)

        query_string = '?owners=%s,%s,' % (owners[0], owners[1])
        request = self.client.get(reverse('v1:multisig-transactions',
                                          kwargs={'address': multisig_confirmation_instance.multisig_transaction.safe})
                                  + query_string, format='json')
        self.assertEqual(request.status_code, status.HTTP_200_OK)
        self.assertEqual(len(request.json()['results']), 1)
        self.assertEqual(len(request.json()['results'][0]['confirmations']), 1)

        query_string = '?owners=%s' % owners[1]
        request = self.client.get(reverse('v1:multisig-transactions',
                                          kwargs={'address': multisig_confirmation_instance.multisig_transaction.safe})
                                  + query_string, format='json')
        self.assertEqual(request.status_code, status.HTTP_200_OK)
        self.assertEqual(len(request.json()['results']), 1)
        self.assertEqual(len(request.json()['results'][0]['confirmations']), 0)

        # Add confirmation for owner1
        multisig_confirmation_instance = MultisigTransactionConfirmationFactory(
            multisig_transaction=multisig_transaction_instance, owner=owners[1])

        query_string = '?owners=%s' % owners[1]
        request = self.client.get(reverse('v1:multisig-transactions',
                                          kwargs={'address': multisig_confirmation_instance.multisig_transaction.safe})
                                  + query_string, format='json')
        self.assertEqual(request.status_code, status.HTTP_200_OK)
        self.assertEqual(len(request.json()['results']), 1)
        self.assertEqual(len(request.json()['results'][0]['confirmations']), 1)

        query_string = '?owners=%s,%s' % (owners[0], owners[1])
        request = self.client.get(reverse('v1:multisig-transactions',
                                          kwargs={'address': multisig_confirmation_instance.multisig_transaction.safe})
                                  + query_string, format='json')
        self.assertEqual(request.status_code, status.HTTP_200_OK)
        self.assertEqual(len(request.json()['results']), 1)
        self.assertEqual(len(request.json()['results'][0]['confirmations']), 2)

    def test_get_multiple_safe_transactions(self):
        multisig_transaction_instance = MultisigTransactionFactory()
        request = self.client.get(reverse('v1:multisig-transactions',
                                          kwargs={'address': multisig_transaction_instance.safe}),
                                  format='json')
        self.assertEqual(request.status_code, status.HTTP_200_OK)
        self.assertEqual(len(request.json()['results']), 1)
        self.assertEqual(len(request.json()['results'][0]['confirmations']), 0)

        multisig_transaction_instance = MultisigTransactionFactory()
        request = self.client.get(reverse('v1:multisig-transactions',
                                          kwargs={'address': multisig_transaction_instance.safe}),
                                  format='json')
        self.assertEqual(request.status_code, status.HTTP_200_OK)
        self.assertEqual(len(request.json()['results']), 2)
        self.assertEqual(len(request.json()['results'][0]['confirmations']), 0)
        self.assertEqual(len(request.json()['results'][1]['confirmations']), 0)

        generate_multisig_transactions(quantity=200)
        request = self.client.get(reverse('v1:multisig-transactions',
                                          kwargs={'address': multisig_transaction_instance.safe}),
                                  format='json')
        self.assertEqual(request.status_code, status.HTTP_200_OK)
        self.assertEqual(request.json()['count'], MultisigTransaction.objects.all().count())

    def test_hex_data(self):
        safe_address, safe_instance, owners, _, _, threshold = self.deploy_test_safe()
        safe_nonce = self.safe_service.retrieve_nonce(safe_address)
        self.assertEqual(safe_nonce, 0)

        # Get removeOwner transaction data
        call_data_owner1 = safe_instance.encodeABI(fn_name='removeOwner', args=[owners[0], owners[1], threshold - 1])

        to = safe_address
        value = self.WITHDRAW_AMOUNT
        data = HexBytes(call_data_owner1)
        operation = SafeOperation.CALL.value
        safe_tx_gas = 500000
        data_gas = 500000
        gas_price = 1
        gas_token = NULL_ADDRESS
        refund_receiver = NULL_ADDRESS
        nonce = safe_nonce
        safe_tx_hash = self.safe_service.get_hash_for_safe_tx(safe_address, to, value, data, operation, safe_tx_gas,
                                                              data_gas, gas_price, gas_token, refund_receiver, nonce)

        sender = owners[0]
        tx_hash_owner0 = safe_instance.functions.approveHash(safe_tx_hash).transact({'from': sender})
        is_approved = safe_instance.functions.approvedHashes(sender, safe_tx_hash).call()
        self.assertTrue(is_approved)

        # Call API
        transaction_data = {
            'safe': safe_address,
            'to': to,
            'value': value,
            'data': data.hex(),
            'operation': operation,
            'nonce': nonce,
            'safe_tx_gas': safe_tx_gas,
            'data_gas': data_gas,
            'gas_price': gas_price,
            'contract_transaction_hash': safe_tx_hash.hex(),
            'transaction_hash': tx_hash_owner0.hex(),
            'block_number': 0,
            'block_date_time': datetime.datetime.now(),
            'sender': sender,
            'type': 'confirmation'
        }

        request = self.client.post(reverse('v1:multisig-transactions', kwargs={'address': safe_address}),
                                   data=transaction_data, format='json')
        print(request.content)
        self.assertEqual(request.status_code, status.HTTP_202_ACCEPTED)

        # Get multisig transaction data
        request = self.client.get(reverse('v1:multisig-transactions', kwargs={'address': safe_address}),
                                  format='json')
        self.assertEqual(request.status_code, status.HTTP_200_OK)
        self.assertEqual(len(request.json()['results']), 1)
        self.assertTrue(request.json()['results'][0]['to'].startswith('0x'))
        self.assertTrue(request.json()['results'][0]['data'].startswith('0x'))
        self.assertTrue(request.json()['results'][0]['confirmations'][0]['owner'].startswith('0x'))
        self.assertTrue(request.json()['results'][0]['confirmations'][0]['transactionHash'].startswith('0x'))
