#include <sys/socket.h>
#include <linux/vm_sockets.h>
#include <iostream>
#include <chrono>
#include <thread>
#include <cstring>

template <typename... Args>
std::string m3_string_format(const std::string &format, Args... args)
{
    int size_s = std::snprintf(nullptr, 0, format.c_str(), args...) + 1; // Extra space for '\0'
    if (size_s <= 0)
    {
        throw std::runtime_error("Error during formatting.");
    }
    auto size = static_cast<size_t>(size_s);
    auto buf = std::make_unique<char[]>(size);
    std::snprintf(buf.get(), size, format.c_str(), args...);
    return std::string(buf.get(), buf.get() + size - 1); // We don't want the '\0' inside
}

int main(int argc, char *argv[])
{


   if (argc < 2) {
            fprintf(stderr, "syntax: %s key(s)\n", argv[0]);
            return 1;
    }

    char buffer[4096];
    int s = socket(AF_VSOCK, SOCK_STREAM, 0);

    struct sockaddr_vm addr;
    memset(&addr, 0, sizeof(struct sockaddr_vm));
    addr.svm_family = AF_VSOCK;
    addr.svm_port = 9999;
    addr.svm_cid = VMADDR_CID_HOST;

    connect(s, (struct sockaddr *)&addr, sizeof(struct sockaddr_vm));

    std::string out;
    for (int i = 1 ; i < argc; i++) {
	out += m3_string_format("%s ", argv[i]);
    }
    out += "\n";
 
    send(s, out.c_str(), out.length(), 0);

    char buf[4096] = { 0 };
    size_t msg_len = recv(s, &buf, 4096, 0);
    if (msg_len < sizeof(buffer) * sizeof(char)) {
	    printf("Value: %.*s", (int)msg_len, buf);
    	return EXIT_SUCCESS;
    } else {
	return EXIT_FAILURE;
    }


}

