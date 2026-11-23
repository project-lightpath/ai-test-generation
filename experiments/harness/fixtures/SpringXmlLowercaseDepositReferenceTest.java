package com.redhat.lightwell.service;

import java.math.BigDecimal;
import java.util.Optional;

import com.redhat.lightwell.model.Account;
import com.redhat.lightwell.model.AccountType;
import com.redhat.lightwell.model.Customer;
import com.redhat.lightwell.model.Transaction;
import com.redhat.lightwell.model.TransactionType;
import com.redhat.lightwell.model.dto.StatementImportResponse;
import com.redhat.lightwell.repository.AccountRepository;
import com.redhat.lightwell.repository.TransactionRepository;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class SpringXmlLowercaseDepositReferenceTest {

    @Test
    void lowercaseDepositCreditsTheAccount() {
        AccountRepository accounts = mock(AccountRepository.class);
        TransactionRepository transactions = mock(TransactionRepository.class);
        AuditService audit = mock(AuditService.class);
        StatementImportService service = new StatementImportService(accounts, transactions, audit);
        Customer customer = new Customer("Test", "User", "test@example.invalid", "555-0100");
        Account account = new Account("ACCOUNT-001", AccountType.CHECKING,
                new BigDecimal("1000.00"), customer);
        when(accounts.findById(1L)).thenReturn(Optional.of(account));
        when(transactions.save(any(Transaction.class)))
                .thenAnswer(invocation -> invocation.getArgument(0));

        String xml = "<statement><transaction><type>deposit</type>"
                + "<amount>100.00</amount></transaction></statement>";
        StatementImportResponse response = service.importStatement(1L, xml);

        ArgumentCaptor<Transaction> saved = ArgumentCaptor.forClass(Transaction.class);
        verify(transactions).save(saved.capture());
        assertThat(response.getTransactionsImported()).isEqualTo(1);
        assertThat(account.getBalance()).isEqualByComparingTo("1100.00");
        assertThat(saved.getValue().getTransactionType()).isEqualTo(TransactionType.DEPOSIT);
    }
}
