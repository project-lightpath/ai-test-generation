package com.projectlightpath.fixture.api;

import static io.restassured.RestAssured.given;
import static org.hamcrest.CoreMatchers.is;

import io.quarkus.test.junit.QuarkusTest;
import io.restassured.http.ContentType;
import org.junit.jupiter.api.Test;

@QuarkusTest
class EmptyUserTransferResourceTest {
    @Test
    void rejectsTransferWhenUserIsEmpty() {
        given()
                .contentType(ContentType.JSON)
                .body("{\"requestReference\":\"req-002\",\"user\":\"\"}")
                .when()
                .post("/transfers")
                .then()
                .statusCode(200)
                .body("requestReference", is("req-002"))
                .body("accepted", is(false));
    }
}
